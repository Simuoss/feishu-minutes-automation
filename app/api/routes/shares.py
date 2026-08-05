import logging
import mimetypes

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.core.config import settings
from app.data_model.entity.share import (
    ShareAdminQueryEntity,
    ShareBatchEnsureEntity,
    ShareBatchRevokeEntity,
    ShareBatchUpdateEntity,
    ShareUpdateEntity,
)
from app.dto.feishu.meeting import LocalMediaFileResponse, LocalMeetingDetailResponse
from app.dto.share import (
    ShareBatchCreateRequest,
    ShareBatchCreateResponse,
    ShareBatchItemResponse,
    ShareBatchResultItemResponse,
    ShareBatchResultResponse,
    ShareBatchRevokeRequest,
    ShareBatchUpdateRequest,
    ShareCreateRequest,
    ShareLibraryItemData,
    ShareLibraryKeyStatusData,
    ShareLibraryRequest,
    ShareLibraryResponse,
    ShareListResponse,
    ShareMetaResponse,
    ShareResponse,
    ShareTryKeysRequest,
    ShareUnlockRequest,
    ShareUnlockResponse,
    ShareUpdateRequest,
)
from app.dto.summary import SummaryDetailResponse, SummaryMetaData
from app.service.export_service import export_service
from app.service.http_download import content_disposition
from app.service.meeting_list_service import MeetingListService
from app.service.meeting_storage_service import MeetingStorageService
from app.service.r2_media_service import r2_media_service
from app.service.share_service import (
    ACTION_EXPORT,
    ACTION_MEDIA,
    share_service,
)

logger = logging.getLogger(__name__)

admin_router = APIRouter(tags=["shares"])
guest_router = APIRouter(prefix="/share", tags=["share-guest"])

_list_service = MeetingListService()
_storage = MeetingStorageService()


def _share_response(entity, *, title: str | None = None) -> ShareResponse:
    return ShareResponse(
        id=entity.id,
        share_token=entity.share_token,
        minute_token=entity.minute_token,
        access_mode=entity.access_mode,
        allow_export=entity.allow_export,
        access_key_id=entity.access_key_id,
        created_at=entity.created_at,
        revoked_at=entity.revoked_at,
        url=share_service.share_url(entity.share_token),
        title=title,
    )


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    return ip, ua


def _session_id(request: Request, header_value: str | None) -> str | None:
    if header_value:
        return header_value.strip()
    return (request.query_params.get("share_session") or "").strip() or None


@admin_router.post("/meetings/{minute_token}/shares", response_model=ShareResponse)
async def create_share(minute_token: str, body: ShareCreateRequest) -> ShareResponse:
    try:
        entity = await share_service.create_share(
            minute_token=minute_token,
            access_mode=body.access_mode,
            allow_export=body.allow_export,
            access_key_id=body.access_key_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _share_response(entity)


@admin_router.post("/shares/batch", response_model=ShareBatchCreateResponse)
async def create_shares_batch(body: ShareBatchCreateRequest) -> ShareBatchCreateResponse:
    try:
        items = await share_service.ensure_shares_batch(
            ShareBatchEnsureEntity(
                minute_tokens=body.minute_tokens,
                access_mode=body.access_mode,
                allow_export=body.allow_export,
                access_key_id=body.access_key_id,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ShareBatchCreateResponse(
        items=[
            ShareBatchItemResponse(
                minute_token=item.minute_token,
                url=share_service.share_url(item.share.share_token) if item.share else None,
                reused=item.reused,
                error=item.error,
            )
            for item in items
        ]
    )


@admin_router.get("/meetings/{minute_token}/shares", response_model=ShareListResponse)
async def list_shares(minute_token: str) -> ShareListResponse:
    items = await share_service.list_shares(minute_token)
    return ShareListResponse(items=[_share_response(i) for i in items])


def _parse_bool_query(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "y"}:
        return True
    if lowered in {"0", "false", "no", "n"}:
        return False
    raise HTTPException(status_code=400, detail="布尔筛选参数无效")


@admin_router.get("/shares", response_model=ShareListResponse)
async def list_all_shares(
    include_revoked: bool = Query(default=False),
    status: str | None = Query(default=None, description="ACTIVE / REVOKED / ALL"),
    minute_token: str | None = Query(default=None),
    q: str | None = Query(default=None, description="按会议名 / token 模糊筛选"),
    access_key_id: int | None = Query(default=None),
    no_access_key: bool = Query(default=False),
    allow_export: str | None = Query(default=None, description="true / false"),
    access_mode: str | None = Query(default=None, description="PUBLIC / KEY_REQUIRED"),
    created_from: int | None = Query(default=None, description="创建时间起（毫秒）"),
    created_to: int | None = Query(default=None, description="创建时间止（毫秒）"),
    sort_by: str = Query(
        default="CREATED_AT",
        description="CREATED_AT / TITLE / ACCESS_KEY_ID / ALLOW_EXPORT / ACCESS_MODE",
    ),
    sort_order: str = Query(default="DESC", description="ASC / DESC"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ShareListResponse:
    resolved_status = (status or ("ALL" if include_revoked else "ACTIVE")).upper()
    if resolved_status not in {"ACTIVE", "REVOKED", "ALL"}:
        raise HTTPException(status_code=400, detail="status 只能是 ACTIVE / REVOKED / ALL")
    if access_mode is not None and access_mode.upper() not in {
        "PUBLIC",
        "KEY_REQUIRED",
    }:
        raise HTTPException(status_code=400, detail="access_mode 无效")
    result = await share_service.list_all_shares(
        ShareAdminQueryEntity(
            status=resolved_status,
            minute_token=minute_token,
            q=q,
            access_key_id=access_key_id,
            no_access_key=no_access_key,
            allow_export=_parse_bool_query(allow_export),
            access_mode=access_mode.upper() if access_mode else None,
            created_from=created_from,
            created_to=created_to,
            sort_by=sort_by.upper(),
            sort_order=sort_order.upper(),
            limit=limit,
            offset=offset,
        )
    )
    return ShareListResponse(
        items=[_share_response(i.share, title=i.title) for i in result.items],
        total=result.total,
    )


@admin_router.patch("/shares/{share_id}", response_model=ShareResponse)
async def update_share(share_id: int, body: ShareUpdateRequest) -> ShareResponse:
    if (
        body.access_mode is None
        and body.allow_export is None
        and body.access_key_id is None
    ):
        raise HTTPException(status_code=400, detail="至少提供一个要修改的字段")
    try:
        entity = await share_service.update_share(
            ShareUpdateEntity(
                id=share_id,
                access_mode=body.access_mode,
                allow_export=body.allow_export,
                access_key_id=body.access_key_id,
            )
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _share_response(entity)


def _batch_result_response(result) -> ShareBatchResultResponse:
    return ShareBatchResultResponse(
        items=[
            ShareBatchResultItemResponse(
                id=item.id,
                ok=item.ok,
                error=item.error,
                share=_share_response(item.share) if item.share else None,
            )
            for item in result.items
        ],
        success_count=result.success_count,
        failed_count=result.failed_count,
    )


@admin_router.post("/shares/batch-update", response_model=ShareBatchResultResponse)
async def batch_update_shares(body: ShareBatchUpdateRequest) -> ShareBatchResultResponse:
    try:
        result = await share_service.batch_update_shares(
            ShareBatchUpdateEntity(
                share_ids=body.share_ids,
                access_mode=body.access_mode,
                allow_export=body.allow_export,
                access_key_id=body.access_key_id,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _batch_result_response(result)


@admin_router.post("/shares/batch-revoke", response_model=ShareBatchResultResponse)
async def batch_revoke_shares(body: ShareBatchRevokeRequest) -> ShareBatchResultResponse:
    try:
        result = await share_service.batch_revoke_shares(
            ShareBatchRevokeEntity(share_ids=body.share_ids)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _batch_result_response(result)


@admin_router.delete("/shares/{share_id}", response_model=ShareResponse)
async def revoke_share(share_id: int) -> ShareResponse:
    try:
        entity = await share_service.revoke_share(share_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _share_response(entity)


@guest_router.post("/library", response_model=ShareLibraryResponse)
async def share_library(body: ShareLibraryRequest) -> ShareLibraryResponse:
    """根据本机保存的密钥（及可选已知 share_token）一次列出可访问分享。"""
    if not body.keys and not body.share_tokens:
        return ShareLibraryResponse(items=[], keys=[])
    result = await share_service.resolve_library(
        keys=body.keys,
        share_tokens=body.share_tokens,
    )
    return ShareLibraryResponse(
        items=[
            ShareLibraryItemData(
                share_token=i.share_token,
                minute_token=i.minute_token,
                title=i.title,
                access_mode=i.access_mode,
                allow_export=i.allow_export,
                url=i.url,
                matched_key_prefix=i.matched_key_prefix,
                source=i.source,
            )
            for i in result.items
        ],
        keys=[
            ShareLibraryKeyStatusData(
                key_prefix=k.key_prefix,
                status=k.status,
                share_count=k.share_count,
            )
            for k in result.keys
        ],
    )


@guest_router.get("/{share_token}/meta", response_model=ShareMetaResponse)
async def share_meta(share_token: str) -> ShareMetaResponse:
    try:
        meta = await share_service.get_meta(share_token)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ShareMetaResponse(**meta)


@guest_router.post("/{share_token}/unlock", response_model=ShareUnlockResponse)
async def share_unlock(
    share_token: str, body: ShareUnlockRequest, request: Request
) -> ShareUnlockResponse:
    ip, ua = _client_meta(request)
    try:
        session, prefix = await share_service.unlock_with_meta(
            share_token, body.key, ip=ip, user_agent=ua
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return ShareUnlockResponse(
        share_session=session.session_id,
        allow_export=session.allow_export,
        minute_token=session.minute_token,
        matched_key_prefix=prefix,
    )


@guest_router.post("/{share_token}/try-keys", response_model=ShareUnlockResponse)
async def share_try_keys(
    share_token: str, body: ShareTryKeysRequest, request: Request
) -> ShareUnlockResponse:
    """一次提交多把本机密钥，命中即解锁，避免前端逐把试钥往返。"""
    ip, ua = _client_meta(request)
    try:
        session, prefix = await share_service.try_unlock_with_keys(
            share_token, body.keys, ip=ip, user_agent=ua
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return ShareUnlockResponse(
        share_session=session.session_id,
        allow_export=session.allow_export,
        minute_token=session.minute_token,
        matched_key_prefix=prefix,
    )


async def _authorized_share(
    share_token: str,
    request: Request,
    x_share_session: str | None,
    *,
    action: str,
    require_export: bool = False,
):
    ip, ua = _client_meta(request)
    try:
        return await share_service.resolve_access(
            share_token,
            _session_id(request, x_share_session),
            action=action,
            ip=ip,
            user_agent=ua,
            require_export=require_export,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@guest_router.get("/{share_token}/detail", response_model=LocalMeetingDetailResponse)
async def share_detail(
    share_token: str,
    request: Request,
    x_share_session: str | None = Header(default=None),
) -> LocalMeetingDetailResponse:
    share = await _authorized_share(
        share_token, request, x_share_session, action="VIEW"
    )
    detail = _list_service.get_local_meeting(share.minute_token)
    if detail is None:
        raise HTTPException(status_code=404, detail="本地未找到该会议资源")

    base = settings.api_public_base.rstrip("/")
    signed_video = await r2_media_service.presign_share_video(share.minute_token)
    media_files: list[LocalMediaFileResponse] = []
    for mf in detail.get("media_files", []):
        local_url = f"{base}/api/v1/share/{share_token}/media/{mf['name']}"
        url = signed_video if mf.get("kind") == "video" and signed_video else local_url
        media_files.append(
            LocalMediaFileResponse(
                name=mf["name"],
                kind=mf["kind"],
                url=url,
            )
        )
    has_video = bool(detail.get("has_video"))
    if signed_video and not any(m.kind == "video" for m in media_files):
        video_meta = r2_media_service.read_meta(share.minute_token).get("video") or {}
        media_files.append(
            LocalMediaFileResponse(
                name=str(video_meta.get("source_name") or "video.mp4"),
                kind="video",
                url=signed_video,
            )
        )
        has_video = True
    return LocalMeetingDetailResponse(
        minute_token=detail["minute_token"],
        title=detail.get("title"),
        duration_ms=detail.get("duration_ms"),
        has_video=has_video,
        media_files=media_files,
        transcript=detail.get("transcript"),
        downloaded_at=detail.get("downloaded_at"),
    )


@guest_router.get("/{share_token}/summary", response_model=SummaryDetailResponse)
async def share_summary(
    share_token: str,
    request: Request,
    x_share_session: str | None = Header(default=None),
) -> SummaryDetailResponse:
    share = await _authorized_share(
        share_token, request, x_share_session, action="VIEW"
    )
    detail = _storage.read_summary(share.minute_token)
    if detail is None:
        raise HTTPException(status_code=404, detail="该会议尚未生成纪要")
    meta = detail.get("meta") or {}
    # 分享页图片走访客 assets 路由：前端用相对路径时由 meeting 逻辑拼 API；
    # 这里仍返回原始 markdown，前端 resolve 到 /share/.../assets/
    return SummaryDetailResponse(
        minute_token=detail["minute_token"],
        content=detail["content"],
        meta=SummaryMetaData(**{k: v for k, v in meta.items() if k in SummaryMetaData.model_fields}),
    )


@guest_router.get("/{share_token}/summary/assets/{filename}")
async def share_summary_asset(
    share_token: str,
    filename: str,
    request: Request,
    x_share_session: str | None = Header(default=None),
) -> Response:
    share = await _authorized_share(
        share_token, request, x_share_session, action=ACTION_MEDIA
    )
    signed = await r2_media_service.presign_asset(share.minute_token, filename)
    if signed:
        return RedirectResponse(url=signed, status_code=302)

    path = _storage.resolve_asset_path(share.minute_token, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="配图不存在")
    media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return FileResponse(path, media_type=media_type)


@guest_router.get("/{share_token}/media/{filename}")
async def share_media(
    share_token: str,
    filename: str,
    request: Request,
    x_share_session: str | None = Header(default=None),
) -> FileResponse:
    share = await _authorized_share(
        share_token, request, x_share_session, action=ACTION_MEDIA
    )
    path = _storage.resolve_media_path(share.minute_token, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    media_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(path, media_type=media_type or "application/octet-stream", filename=path.name)


@guest_router.get("/{share_token}/export/summary")
async def share_export_summary(
    share_token: str,
    request: Request,
    format: str = Query(..., pattern="^(pdf|docx|md)$"),
    x_share_session: str | None = Header(default=None),
) -> Response:
    share = await _authorized_share(
        share_token,
        request,
        x_share_session,
        action=ACTION_EXPORT,
        require_export=True,
    )
    try:
        data, filename, media_type = export_service.export_summary(share.minute_token, format)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("分享纪要导出失败 token=%s format=%s: %s", share.minute_token, format, exc)
        raise HTTPException(status_code=500, detail=f"导出失败：{exc}") from exc
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": content_disposition(filename)},
    )


@guest_router.get("/{share_token}/export/transcript")
async def share_export_transcript(
    share_token: str,
    request: Request,
    format: str = Query(..., pattern="^(md|txt)$"),
    x_share_session: str | None = Header(default=None),
) -> Response:
    share = await _authorized_share(
        share_token,
        request,
        x_share_session,
        action=ACTION_EXPORT,
        require_export=True,
    )
    try:
        data, filename, media_type = export_service.export_transcript(share.minute_token, format)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": content_disposition(filename)},
    )
