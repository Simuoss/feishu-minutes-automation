import logging
import mimetypes
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse

from app.core.config import settings
from app.dto.feishu.meeting import (
    CloudMeetingItemResponse,
    CloudMeetingListResponse,
    DownloadMeetingsRequest,
    DownloadMeetingsResponse,
    DownloadProgressBatchRequest,
    DownloadProgressBatchResponse,
    DownloadProgressResponse,
    DownloadResultItemResponse,
    LocalMediaFileResponse,
    LocalMeetingDetailResponse,
)
from app.integrations.feishu.user_auth import UserAuthRequiredError
from app.service.download_progress_store import download_progress_store
from app.service.meeting_download_service import MeetingDownloadService
from app.service.meeting_list_service import MeetingListService
from app.service.meeting_storage_service import MeetingStorageService
from app.service.r2_media_service import r2_media_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["meetings"])

_list_service = MeetingListService()
_download_service = MeetingDownloadService()
_storage = MeetingStorageService()


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace(" ", "T")
    if len(normalized) == 16:
        normalized = f"{normalized}:00"
    return datetime.fromisoformat(normalized)


@router.get("/cloud", response_model=CloudMeetingListResponse)
async def list_cloud_meetings(
    query: str | None = None,
    keyword: str | None = None,
    start_time: str | None = Query(default=None, description="ISO 或 datetime-local 格式"),
    end_time: str | None = None,
    days: int | None = Query(default=None, ge=1, le=366),
    min_duration_minutes: float | None = Query(default=None, ge=0),
    max_duration_minutes: float | None = Query(default=None, ge=0),
    page_size: int = Query(default=20, ge=1, le=30),
    page_token: str | None = None,
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> CloudMeetingListResponse:
    try:
        min_duration_ms = (
            int(min_duration_minutes * 60_000) if min_duration_minutes is not None else None
        )
        max_duration_ms = (
            int(max_duration_minutes * 60_000) if max_duration_minutes is not None else None
        )
        data = await _list_service.list_cloud_meetings(
            query=query,
            keyword=keyword,
            start_time=_parse_optional_datetime(start_time),
            end_time=_parse_optional_datetime(end_time),
            days=days,
            min_duration_ms=min_duration_ms,
            max_duration_ms=max_duration_ms,
            page_size=page_size,
            page_token=page_token,
            sort_order=sort_order,
        )
    except UserAuthRequiredError as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "message": str(exc),
                "login_url": "/api/v1/auth/feishu/login",
            },
        ) from exc
    except Exception as exc:
        logger.error("拉取云端妙记列表失败，怀疑 search 权限未开通或 Token 类型不匹配: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return CloudMeetingListResponse(
        items=[CloudMeetingItemResponse(**item) for item in data["items"]],
        total=data["total"],
        filtered_total=data.get("filtered_total"),
        has_more=data["has_more"],
        page_token=data["page_token"],
    )


async def _run_downloads(
    minute_tokens: list[str],
    skip_if_completed: bool,
    redownload_media: bool = False,
    redownload_transcript: bool = False,
) -> None:
    await _download_service.download_many(
        minute_tokens,
        skip_if_completed=skip_if_completed,
        redownload_media=redownload_media,
        redownload_transcript=redownload_transcript,
    )


def _result_to_response(result) -> DownloadResultItemResponse:
    return DownloadResultItemResponse(
        minute_token=result.minute_token,
        record_id=result.record_id,
        status=result.status,
        error_message=result.error_message,
        error_code=result.error_code,
        needs_app_scope=result.needs_app_scope,
        scope_auth_url=result.scope_auth_url,
        needs_user_login=result.needs_user_login,
    )


@router.post("/download", response_model=DownloadMeetingsResponse)
async def download_meetings(
    body: DownloadMeetingsRequest,
    background_tasks: BackgroundTasks,
    sync: bool = Query(default=False, description="为 true 时同步等待下载完成"),
) -> DownloadMeetingsResponse:
    tokens = list(dict.fromkeys(t for t in body.minute_tokens if t.strip()))
    if not tokens:
        raise HTTPException(status_code=400, detail="minute_tokens 不能为空")

    if sync:
        results = await _download_service.download_many(
            tokens,
            skip_if_completed=body.skip_if_completed,
            redownload_media=body.redownload_media,
            redownload_transcript=body.redownload_transcript,
        )
    else:
        background_tasks.add_task(
            _run_downloads,
            tokens,
            body.skip_if_completed,
            body.redownload_media,
            body.redownload_transcript,
        )
        results = [
            DownloadResultItemResponse(minute_token=t, status="DOWNLOADING")
            for t in tokens
        ]
        return DownloadMeetingsResponse(results=results)

    return DownloadMeetingsResponse(results=[_result_to_response(r) for r in results])


@router.get("/download/progress/{minute_token}", response_model=DownloadProgressResponse)
async def get_download_progress(minute_token: str) -> DownloadProgressResponse:
    progress = download_progress_store.get(minute_token)
    if progress is None:
        raise HTTPException(status_code=404, detail="暂无该会议的下载进度")
    return DownloadProgressResponse(
        minute_token=progress.minute_token,
        percent=progress.percent,
        stage=progress.stage,
        status=progress.status,
        error_message=progress.error_message,
    )


@router.post("/download/progress/batch", response_model=DownloadProgressBatchResponse)
async def get_download_progress_batch(
    body: DownloadProgressBatchRequest,
) -> DownloadProgressBatchResponse:
    items: list[DownloadProgressResponse] = []
    for token in body.minute_tokens:
        progress = download_progress_store.get(token)
        if progress is None:
            continue
        items.append(
            DownloadProgressResponse(
                minute_token=progress.minute_token,
                percent=progress.percent,
                stage=progress.stage,
                status=progress.status,
                error_message=progress.error_message,
            )
        )
    return DownloadProgressBatchResponse(items=items)


@router.get("/local/{minute_token}", response_model=LocalMeetingDetailResponse)
async def get_local_meeting(minute_token: str) -> LocalMeetingDetailResponse:
    detail = _list_service.get_local_meeting(minute_token)
    if detail is None:
        raise HTTPException(status_code=404, detail="本地未找到该会议资源")

    media_files = list(detail.get("media_files") or [])
    signed_video = await r2_media_service.presign_share_video(minute_token)
    has_video = bool(detail.get("has_video"))
    if signed_video:
        has_video = True
        video_meta = (r2_media_service.read_meta(minute_token).get("video") or {})
        source_name = str(video_meta.get("source_name") or "video.mp4")
        replaced = False
        for mf in media_files:
            if mf.get("kind") == "video":
                mf["url"] = signed_video
                replaced = True
        if not replaced:
            media_files.append(
                {"name": source_name, "kind": "video", "url": signed_video}
            )

    return LocalMeetingDetailResponse(
        minute_token=detail["minute_token"],
        title=detail.get("title"),
        duration_ms=detail.get("duration_ms"),
        has_video=has_video,
        media_files=[LocalMediaFileResponse(**mf) for mf in media_files],
        transcript=detail.get("transcript"),
        downloaded_at=detail.get("downloaded_at"),
    )


@router.get("/local/{minute_token}/media/{filename}")
async def get_local_media(minute_token: str, filename: str):
    """本地媒体；视频若已上云则 302 到 R2。"""
    path = _storage.resolve_media_path(minute_token, filename)
    if path is None:
        signed = await r2_media_service.presign_share_video(minute_token)
        if signed:
            return RedirectResponse(url=signed, status_code=302)
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    # 视频已在云上时优先云播放，并顺带清理残留本地原片
    suffix = path.suffix.lower()
    if suffix in {".mp4", ".mov", ".webm", ".mkv"} and r2_media_service.video_ready(
        minute_token
    ):
        signed = await r2_media_service.presign_share_video(minute_token)
        if signed:
            r2_media_service.purge_local_videos(minute_token)
            return RedirectResponse(url=signed, status_code=302)
    media_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(path, media_type=media_type or "application/octet-stream", filename=path.name)
