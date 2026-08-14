import json
import logging
import mimetypes
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from app.core.auth_context import require_user_id
from app.dto.summary import (
    GenerateSummaryRequest,
    GenerateSummaryResponse,
    RedactionAbandonRequest,
    RedactionActionResponse,
    RedactionApproveRequest,
    RedactionAuditResponse,
    RedactionFigureData,
    SummaryDetailResponse,
    SummaryMetaData,
    SummaryMetricsBatchRequest,
    SummaryMetricsBatchResponse,
    SummaryMetricsResponse,
    SummaryProgressBatchRequest,
    SummaryProgressBatchResponse,
    SummaryProgressResponse,
)
from app.service.meeting_storage_service import MeetingStorageService
from app.service.ownership import (
    assert_meeting_progress_visible,
    assert_meeting_readable,
    resolve_resource_owner,
)
from app.service.r2_media_service import r2_media_service
from app.service.redaction_access import assert_asset_safe_to_serve
from app.service.redaction_review_service import redaction_review_service
from app.service.summary_event_broker import SummaryStatus, summary_broker
from app.service.summary_generation_service import summary_generation_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["summaries"])

_summary_service = summary_generation_service
_storage = MeetingStorageService()
_review = redaction_review_service


def _meta_to_dto(meta: dict[str, Any]) -> SummaryMetaData:
    return SummaryMetaData(
        **{k: v for k, v in meta.items() if k in SummaryMetaData.model_fields}
    )


async def _build_metrics(
    minute_token: str, *, owner_user_id: int
) -> SummaryMetricsResponse:
    from app.service.metadata_db_service import read_r2_meta, read_summary_meta

    try:
        meta = await read_summary_meta(
            minute_token, owner_user_id=owner_user_id
        ) or {}
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "从 DB 读纪要指标失败 token=%s；怀疑 schema 未就绪。err=%s",
            minute_token,
            exc,
        )
        meta = {}

    suspicious = meta.get("anchor_suspicious")
    if isinstance(suspicious, list):
        suspicious_count = len(suspicious)
    elif isinstance(suspicious, int):
        suspicious_count = suspicious
    else:
        suspicious_count = 0

    input_tokens = meta.get("input_tokens")
    output_tokens = meta.get("output_tokens")
    total = None
    if isinstance(input_tokens, int) or isinstance(output_tokens, int):
        total = int(input_tokens or 0) + int(output_tokens or 0)

    r2_count = None
    try:
        r2_meta = await read_r2_meta(
            minute_token, owner_user_id=owner_user_id
        ) or {"assets": {}}
        assets = r2_meta.get("assets") or {}
        r2_count = len(assets) if isinstance(assets, dict) else None
    except Exception:  # noqa: BLE001
        r2_count = None

    has_summary = _storage.has_summary(
        minute_token, owner_user_id=owner_user_id
    ) or bool(meta.get("generated_at") or meta.get("summary_chars"))
    return SummaryMetricsResponse(
        minute_token=minute_token,
        has_summary=has_summary,
        model=meta.get("model"),
        input_tokens=input_tokens if isinstance(input_tokens, int) else None,
        output_tokens=output_tokens if isinstance(output_tokens, int) else None,
        total_tokens=total,
        elapsed_seconds=meta.get("elapsed_seconds"),
        attempts=meta.get("attempts"),
        truncated=meta.get("truncated"),
        transcript_chars=meta.get("transcript_chars"),
        summary_chars=meta.get("summary_chars"),
        anchor_total=meta.get("anchor_total"),
        anchor_aligned=meta.get("anchor_aligned"),
        anchor_suspicious_count=suspicious_count,
        figure_planned=meta.get("figure_planned"),
        figure_for_writing=meta.get("figure_for_writing"),
        figure_used=meta.get("figure_used"),
        figure_scanned=meta.get("figure_scanned"),
        figure_sensitive=meta.get("figure_sensitive"),
        figure_redacted=meta.get("figure_redacted"),
        figure_abandoned=meta.get("figure_abandoned"),
        generated_at=meta.get("generated_at"),
        run_mode=meta.get("run_mode"),
        scene=meta.get("scene"),
        r2_asset_count=r2_count,
    )


def _audit_to_response(minute_token: str, audit: dict[str, Any]) -> RedactionAuditResponse:
    figures: list[RedactionFigureData] = []
    for item in audit.get("figures") or []:
        if not isinstance(item, dict):
            continue
        figure_id = str(item.get("figure_id") or "")
        status = str(item.get("status") or "CLEAN").upper()
        asset_rel = item.get("asset_relative")
        asset_url = None
        # 永不下发原图；仅 CLEAN / 已打码 可给成文配图 URL
        if asset_rel and status in {"CLEAN", "REDACTED", "MANUAL_APPROVED"}:
            name = str(asset_rel).rsplit("/", 1)[-1]
            asset_url = f"/api/v1/meetings/{minute_token}/summary/assets/{name}"
        figures.append(
            RedactionFigureData(
                figure_id=figure_id,
                sensitive=bool(item.get("sensitive")),
                status=status,
                regions=item.get("regions") or [],
                attempts=item.get("attempts") or [],
                abandon_reason=item.get("abandon_reason"),
                original_relative=None,
                asset_relative=asset_rel if asset_url else None,
                original_url=None,
                asset_url=asset_url,
            )
        )
    return RedactionAuditResponse(
        minute_token=minute_token,
        saved_at=audit.get("saved_at"),
        scanned=int(audit.get("scanned") or 0),
        sensitive=int(audit.get("sensitive") or 0),
        redacted=int(audit.get("redacted") or 0),
        abandoned=int(audit.get("abandoned") or 0),
        figures=figures,
    )


@router.post("/{minute_token}/summary", response_model=GenerateSummaryResponse)
async def generate_summary(
    minute_token: str,
    body: GenerateSummaryRequest,
    request: Request,
    sync: bool = Query(default=False, description="为 true 时同步等待生成完成，耗时可达数分钟"),
    owner_user_id: int | None = None,
) -> GenerateSummaryResponse:
    require_user_id(request)
    owner = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )
    if body.mode != "R2_SYNC" and await _storage.read_transcript_async(
        minute_token, owner_user_id=owner
    ) is None:
        raise HTTPException(status_code=404, detail="本地没有该会议的转写文本，请先下载")

    if sync:
        result = await _summary_service.generate(
            minute_token, owner_user_id=owner, force=body.force, mode=body.mode
        )
        return GenerateSummaryResponse(
            minute_token=result.minute_token,
            status=result.status,
            char_count=result.char_count,
            anchor_total=result.anchor_total,
            anchor_aligned=result.anchor_aligned,
            anchor_suspicious=result.anchor_suspicious,
            figure_planned=result.figure_planned,
            figure_used=result.figure_used,
            truncated=result.truncated,
            elapsed_seconds=round(result.elapsed_seconds, 1),
            error_message=result.error_message,
            mode=result.mode,
        )

    from app.service.pipeline_queue import JOB_SUMMARY, enqueue_job
    from app.service.summary_event_broker import summary_broker as _broker

    await enqueue_job(
        minute_token,
        owner_user_id=owner,
        job_type=JOB_SUMMARY,
        mode=body.mode,
        force=body.force,
    )
    _broker.bind(minute_token, owner_user_id=owner).queue(position=0)
    return GenerateSummaryResponse(
        minute_token=minute_token,
        status=SummaryStatus.QUEUED.value,
        mode=body.mode,
    )


@router.get("/{minute_token}/summary/progress", response_model=SummaryProgressResponse)
async def get_summary_progress(
    minute_token: str,
    request: Request,
    owner_user_id: int | None = None,
) -> SummaryProgressResponse:
    owner = await assert_meeting_progress_visible(
        request, minute_token, owner_user_id=owner_user_id
    )
    from app.service.pipeline_queue import resolve_progress

    snap = await resolve_progress(minute_token, owner_user_id=owner)
    if snap is None:
        raise HTTPException(status_code=404, detail="暂无该会议的纪要生成进度")
    return SummaryProgressResponse(**snap)


@router.post("/summary/progress/batch", response_model=SummaryProgressBatchResponse)
async def get_summary_progress_batch(
    body: SummaryProgressBatchRequest,
    request: Request,
) -> SummaryProgressBatchResponse:
    items: list[SummaryProgressResponse] = []
    for item in body.items:
        token = (item.minute_token or "").strip()
        if not token:
            continue
        try:
            owner = await resolve_resource_owner(
                request, token, owner_user_id=item.owner_user_id
            )
        except HTTPException:
            continue
        from app.service.pipeline_queue import resolve_progress

        snap = await resolve_progress(token, owner_user_id=owner)
        if snap is None:
            continue
        items.append(SummaryProgressResponse(**snap))
    return SummaryProgressBatchResponse(items=items)


@router.get("/{minute_token}/summary/metrics", response_model=SummaryMetricsResponse)
async def get_summary_metrics(
    minute_token: str,
    request: Request,
    owner_user_id: int | None = None,
) -> SummaryMetricsResponse:
    owner = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )
    return await _build_metrics(minute_token, owner_user_id=owner)


@router.post("/summary/metrics/batch", response_model=SummaryMetricsBatchResponse)
async def get_summary_metrics_batch(
    body: SummaryMetricsBatchRequest,
    request: Request,
    owner_user_id: int | None = None,
) -> SummaryMetricsBatchResponse:
    items: list[SummaryMetricsResponse] = []
    for token in body.minute_tokens:
        try:
            owner = await assert_meeting_readable(
                request, token, owner_user_id=owner_user_id
            )
        except HTTPException:
            continue
        items.append(await _build_metrics(token, owner_user_id=owner))
    return SummaryMetricsBatchResponse(items=items)


@router.get("/{minute_token}/summary/stream")
async def stream_summary(
    minute_token: str,
    request: Request,
    owner_user_id: int | None = None,
) -> StreamingResponse:
    """以 SSE 推送生成状态与正文增量；新连接首帧为当前快照，可中途接入。"""
    owner = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )

    async def event_source() -> AsyncIterator[str]:
        async for item in summary_broker.subscribe(
            minute_token, owner_user_id=owner
        ):
            payload = json.dumps(item["data"], ensure_ascii=False)
            yield f"event: {item['event']}\ndata: {payload}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{minute_token}/summary/assets/{filename}")
async def get_summary_asset(
    minute_token: str,
    filename: str,
    request: Request,
    owner_user_id: int | None = None,
):
    """提供纪要正文里引用的截图；优先 R2 预签名。"""
    owner = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )
    await assert_asset_safe_to_serve(
        minute_token, filename, owner_user_id=owner
    )
    signed = await r2_media_service.presign_asset(
        minute_token, filename, owner_user_id=owner
    )
    if signed:
        return RedirectResponse(url=signed, status_code=302)
    asset_path = _storage.resolve_asset_path(
        minute_token, filename, owner_user_id=owner
    )
    if asset_path is None:
        raise HTTPException(status_code=404, detail="配图不存在")
    media_type = mimetypes.guess_type(asset_path.name)[0] or "image/jpeg"
    return FileResponse(asset_path, media_type=media_type)


@router.get("/{minute_token}/summary", response_model=SummaryDetailResponse)
async def get_summary(
    minute_token: str,
    request: Request,
    owner_user_id: int | None = None,
    inline: bool = Query(
        False, description="为 true 时强制经 API 内嵌正文（不走 R2 直链）"
    ),
) -> SummaryDetailResponse:
    owner = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )
    if not inline:
        content_url = await r2_media_service.presign_summary_text(
            minute_token, owner_user_id=owner
        )
        if content_url:
            from app.service.metadata_db_service import read_summary_meta

            meta = await read_summary_meta(
                minute_token, owner_user_id=owner
            ) or {}
            # 无本地/元数据时仍可能只有 R2 正文
            return SummaryDetailResponse(
                minute_token=minute_token,
                content="",
                content_url=content_url,
                meta=_meta_to_dto(meta) if meta else None,
            )
    detail = await _storage.read_summary_async(
        minute_token, owner_user_id=owner
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="该会议尚未生成纪要")
    meta = detail.get("meta") or {}
    return SummaryDetailResponse(
        minute_token=detail["minute_token"],
        content=detail["content"],
        content_url=None,
        meta=_meta_to_dto(meta),
    )


@router.get("/{minute_token}/redaction/audit", response_model=RedactionAuditResponse)
async def get_redaction_audit(
    minute_token: str,
    request: Request,
    owner_user_id: int | None = None,
) -> RedactionAuditResponse:
    owner = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )
    audit = _review.read_audit(minute_token, owner_user_id=owner)
    if audit is None:
        raise HTTPException(status_code=404, detail="尚无脱敏审计记录，请先生成带图纪要")
    return _audit_to_response(minute_token, audit)


@router.get("/{minute_token}/redaction/originals/{filename}")
async def get_redaction_original(
    minute_token: str, filename: str, request: Request
) -> FileResponse:
    # 原图永不对前端开放，否则脱敏失去意义
    raise HTTPException(status_code=403, detail="脱敏原图不可访问")


@router.post(
    "/{minute_token}/redaction/approve",
    response_model=RedactionActionResponse,
)
async def approve_redaction(
    minute_token: str,
    body: RedactionApproveRequest,
    request: Request,
    owner_user_id: int | None = None,
) -> RedactionActionResponse:
    require_user_id(request)
    owner = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )
    try:
        audit = _review.approve(
            minute_token,
            body.figure_id,
            [r.model_dump() for r in body.regions],
            owner_user_id=owner,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await r2_media_service.sync_assets_safe(minute_token, owner_user_id=owner)
    return RedactionActionResponse(
        minute_token=minute_token,
        figure_id=body.figure_id,
        status="MANUAL_APPROVED",
        message="已按指定区域打码并放行",
        audit=_audit_to_response(minute_token, audit),
    )


@router.post(
    "/{minute_token}/redaction/abandon",
    response_model=RedactionActionResponse,
)
async def abandon_redaction(
    minute_token: str,
    body: RedactionAbandonRequest,
    request: Request,
    owner_user_id: int | None = None,
) -> RedactionActionResponse:
    require_user_id(request)
    owner = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )
    audit = _review.abandon(
        minute_token, body.figure_id, body.reason, owner_user_id=owner
    )
    return RedactionActionResponse(
        minute_token=minute_token,
        figure_id=body.figure_id,
        status="ABANDONED",
        message="已放弃该配图",
        audit=_audit_to_response(minute_token, audit),
    )
