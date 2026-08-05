import json
import logging
import mimetypes
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

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
from app.service.r2_media_service import r2_media_service
from app.service.redaction_review_service import redaction_review_service
from app.service.summary_event_broker import SummaryStatus, summary_broker
from app.service.summary_generation_service import summary_generation_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["summaries"])

_summary_service = summary_generation_service
_storage = MeetingStorageService()
_review = redaction_review_service


async def _run_generation(minute_token: str, force: bool, mode: str) -> None:
    await _summary_service.generate(minute_token, force=force, mode=mode)


def _meta_to_dto(meta: dict[str, Any]) -> SummaryMetaData:
    return SummaryMetaData(
        **{k: v for k, v in meta.items() if k in SummaryMetaData.model_fields}
    )


def _build_metrics(minute_token: str) -> SummaryMetricsResponse:
    detail = _storage.read_summary(minute_token)
    meta = (detail or {}).get("meta") or {}
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
        r2_meta = r2_media_service.read_meta(minute_token)
        assets = r2_meta.get("assets") or {}
        r2_count = len(assets) if isinstance(assets, dict) else None
    except Exception:  # noqa: BLE001
        r2_count = None

    return SummaryMetricsResponse(
        minute_token=minute_token,
        has_summary=detail is not None,
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
        r2_asset_count=r2_count,
    )


def _audit_to_response(minute_token: str, audit: dict[str, Any]) -> RedactionAuditResponse:
    figures: list[RedactionFigureData] = []
    for item in audit.get("figures") or []:
        if not isinstance(item, dict):
            continue
        figure_id = str(item.get("figure_id") or "")
        original_rel = item.get("original_relative")
        asset_rel = item.get("asset_relative")
        original_url = None
        asset_url = None
        if original_rel and _storage.resolve_agent_relative_path(minute_token, str(original_rel)):
            original_url = f"/api/v1/meetings/{minute_token}/redaction/originals/{figure_id}.jpg"
        if asset_rel:
            name = str(asset_rel).rsplit("/", 1)[-1]
            if _storage.resolve_asset_path(minute_token, name):
                asset_url = f"/api/v1/meetings/{minute_token}/summary/assets/{name}"
        figures.append(
            RedactionFigureData(
                figure_id=figure_id,
                sensitive=bool(item.get("sensitive")),
                status=str(item.get("status") or "CLEAN"),
                regions=item.get("regions") or [],
                attempts=item.get("attempts") or [],
                abandon_reason=item.get("abandon_reason"),
                original_relative=original_rel,
                asset_relative=asset_rel,
                original_url=original_url,
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
    background_tasks: BackgroundTasks,
    sync: bool = Query(default=False, description="为 true 时同步等待生成完成，耗时可达数分钟"),
) -> GenerateSummaryResponse:
    if body.mode != "R2_SYNC" and _storage.read_transcript(minute_token) is None:
        raise HTTPException(status_code=404, detail="本地没有该会议的转写文本，请先下载")

    if sync:
        result = await _summary_service.generate(
            minute_token, force=body.force, mode=body.mode
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

    background_tasks.add_task(_run_generation, minute_token, body.force, body.mode)
    return GenerateSummaryResponse(
        minute_token=minute_token,
        status=SummaryStatus.QUEUED.value,
        mode=body.mode,
    )


@router.get("/{minute_token}/summary/progress", response_model=SummaryProgressResponse)
async def get_summary_progress(minute_token: str) -> SummaryProgressResponse:
    channel = summary_broker.get(minute_token)
    if channel is None:
        raise HTTPException(status_code=404, detail="暂无该会议的纪要生成进度")
    return SummaryProgressResponse(**channel.snapshot())


@router.post("/summary/progress/batch", response_model=SummaryProgressBatchResponse)
async def get_summary_progress_batch(
    body: SummaryProgressBatchRequest,
) -> SummaryProgressBatchResponse:
    items: list[SummaryProgressResponse] = []
    for token in body.minute_tokens:
        channel = summary_broker.get(token)
        if channel is None:
            continue
        items.append(SummaryProgressResponse(**channel.snapshot()))
    return SummaryProgressBatchResponse(items=items)


@router.get("/{minute_token}/summary/metrics", response_model=SummaryMetricsResponse)
async def get_summary_metrics(minute_token: str) -> SummaryMetricsResponse:
    return _build_metrics(minute_token)


@router.post("/summary/metrics/batch", response_model=SummaryMetricsBatchResponse)
async def get_summary_metrics_batch(
    body: SummaryMetricsBatchRequest,
) -> SummaryMetricsBatchResponse:
    return SummaryMetricsBatchResponse(
        items=[_build_metrics(token) for token in body.minute_tokens]
    )


@router.get("/{minute_token}/summary/stream")
async def stream_summary(minute_token: str) -> StreamingResponse:
    """以 SSE 推送生成状态与正文增量；新连接首帧为当前快照，可中途接入。"""

    async def event_source() -> AsyncIterator[str]:
        async for item in summary_broker.subscribe(minute_token):
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
async def get_summary_asset(minute_token: str, filename: str) -> FileResponse:
    """提供纪要正文里引用的截图。"""
    asset_path = _storage.resolve_asset_path(minute_token, filename)
    if asset_path is None:
        raise HTTPException(status_code=404, detail="配图不存在")
    media_type = mimetypes.guess_type(asset_path.name)[0] or "image/jpeg"
    return FileResponse(asset_path, media_type=media_type)


@router.get("/{minute_token}/summary", response_model=SummaryDetailResponse)
async def get_summary(minute_token: str) -> SummaryDetailResponse:
    detail = _storage.read_summary(minute_token)
    if detail is None:
        raise HTTPException(status_code=404, detail="该会议尚未生成纪要")
    meta = detail.get("meta") or {}
    return SummaryDetailResponse(
        minute_token=detail["minute_token"],
        content=detail["content"],
        meta=_meta_to_dto(meta),
    )


@router.get("/{minute_token}/redaction/audit", response_model=RedactionAuditResponse)
async def get_redaction_audit(minute_token: str) -> RedactionAuditResponse:
    audit = _review.read_audit(minute_token)
    if audit is None:
        raise HTTPException(status_code=404, detail="尚无脱敏审计记录，请先生成带图纪要")
    return _audit_to_response(minute_token, audit)


@router.get("/{minute_token}/redaction/originals/{filename}")
async def get_redaction_original(minute_token: str, filename: str) -> FileResponse:
    path = _storage.resolve_agent_relative_path(
        minute_token, f"agent/redaction/originals/{filename}"
    )
    if path is None:
        raise HTTPException(status_code=404, detail="脱敏原图不存在")
    media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return FileResponse(path, media_type=media_type)


@router.post(
    "/{minute_token}/redaction/approve",
    response_model=RedactionActionResponse,
)
async def approve_redaction(
    minute_token: str,
    body: RedactionApproveRequest,
) -> RedactionActionResponse:
    try:
        audit = _review.approve(
            minute_token,
            body.figure_id,
            [r.model_dump() for r in body.regions],
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
) -> RedactionActionResponse:
    audit = _review.abandon(minute_token, body.figure_id, body.reason)
    return RedactionActionResponse(
        minute_token=minute_token,
        figure_id=body.figure_id,
        status="ABANDONED",
        message="已放弃该配图",
        audit=_audit_to_response(minute_token, audit),
    )
