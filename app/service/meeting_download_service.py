import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import settings
from app.data_model.entity.meeting_record import (
    MeetingRecordCreateEntity,
    MeetingRecordUpdateEntity,
)
from app.integrations.feishu.errors import MINUTE_NOT_READY_CODE, parse_feishu_api_error
from app.integrations.feishu.minutes_client import FeishuMinutesClient
from app.repository.uow import UnitOfWork
from app.service.download_pool import download_pool
from app.service.download_progress_store import download_progress_store
from app.service.meeting_storage_service import MeetingStorageService
from app.service.r2_media_service import r2_media_service
from app.service.summary_generation_service import summary_generation_service

logger = logging.getLogger(__name__)

MANUAL_EVENT_TYPE = "MANUAL_DOWNLOAD"

# 自动生成任务脱离请求生命周期运行，必须持有引用，否则可能被 GC 中途回收
_auto_summary_tasks: set[asyncio.Task[object]] = set()


@dataclass
class DownloadResult:
    minute_token: str
    record_id: int | None
    status: str
    error_message: str | None = None
    error_code: int | None = None
    needs_app_scope: bool = False
    scope_auth_url: str | None = None
    needs_user_login: bool = False


class MeetingDownloadService:
    """妙记资源下载（事件触发与手动触发共用）。"""

    def __init__(
        self,
        storage: MeetingStorageService | None = None,
        minutes_client: FeishuMinutesClient | None = None,
    ) -> None:
        self._storage = storage or MeetingStorageService()
        self._minutes = minutes_client or FeishuMinutesClient()

    @staticmethod
    def _spawn_auto_summary(minute_token: str) -> None:
        """下载完成后自动排队生成纪要，不阻塞下载流程。"""
        if not settings.summary_auto_generate:
            return
        task: asyncio.Task[object] = asyncio.create_task(
            summary_generation_service.generate(minute_token)
        )
        _auto_summary_tasks.add(task)
        task.add_done_callback(_auto_summary_tasks.discard)
        logger.info("已为妙记 %s 排队自动生成纪要", minute_token)

    @staticmethod
    def _spawn_r2_share_video(minute_token: str) -> None:
        """下载完成后后台压缩分享视频并上传 R2，失败不影响下载结果。"""
        r2_media_service.spawn_share_video(minute_token)

    async def download_minute(
        self,
        minute_token: str,
        *,
        feishu_event_id: str | None = None,
        event_type: str = MANUAL_EVENT_TYPE,
        unique_key: str | None = None,
        skip_if_completed: bool = True,
        redownload_media: bool = False,
        redownload_transcript: bool = False,
        ready_retries: int = 1,
        ready_retry_seconds: float = 0.0,
    ) -> DownloadResult:
        fetch_media = redownload_media or not self._storage.has_media(minute_token)
        fetch_transcript = redownload_transcript or not self._storage.has_transcript(
            minute_token
        )
        if (
            skip_if_completed
            and self._storage.is_persisted(minute_token)
            and not redownload_media
            and not redownload_transcript
        ):
            async with UnitOfWork() as uow:
                assert uow.meeting_records is not None
                existing = await uow.meeting_records.get_latest_by_minute_token(minute_token)
            if existing and existing.status == "COMPLETED":
                logger.info("妙记已本地持久化，跳过重复下载 token=%s", minute_token)
                download_progress_store.complete(minute_token)
                self._spawn_auto_summary(minute_token)
                self._spawn_r2_share_video(minute_token)
                return DownloadResult(
                    minute_token=minute_token,
                    record_id=existing.id,
                    status="COMPLETED",
                )

        # 已落盘的跳过路径不占并发槽；真正拉媒体的任务统一进下载队列
        return await download_pool.submit(
            minute_token,
            lambda: self._download_minute_in_pool(
                minute_token,
                feishu_event_id=feishu_event_id,
                event_type=event_type,
                unique_key=unique_key,
                fetch_media=fetch_media,
                fetch_transcript=fetch_transcript,
                ready_retries=ready_retries,
                ready_retry_seconds=ready_retry_seconds,
            ),
        )

    async def _download_minute_in_pool(
        self,
        minute_token: str,
        *,
        feishu_event_id: str | None,
        event_type: str,
        unique_key: str | None,
        fetch_media: bool,
        fetch_transcript: bool,
        ready_retries: int,
        ready_retry_seconds: float,
    ) -> DownloadResult:
        event_id = feishu_event_id or f"manual-{minute_token}-{uuid.uuid4().hex[:12]}"

        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            dup = await uow.meeting_records.get_by_event_id(event_id)
            if dup:
                return DownloadResult(
                    minute_token=minute_token,
                    record_id=dup.id,
                    status=dup.status,
                    error_message=dup.error_message,
                )

            from app.service.metadata_db_service import get_admin_user_id

            owner_id = await get_admin_user_id()
            record = await uow.meeting_records.create(
                MeetingRecordCreateEntity(
                    feishu_event_id=event_id,
                    event_type=event_type,
                    unique_key=unique_key,
                    minute_token=minute_token,
                    status="DOWNLOADING",
                    storage_path=str(self._storage.get_meeting_dir(minute_token)),
                    owner_user_id=owner_id,
                    storage_root_relpath=minute_token,
                )
            )
            record_id = record.id
            if record_id is None:
                raise RuntimeError("创建下载记录失败，未返回 record id")
            await uow.commit()

        download_progress_store.start(minute_token)
        from app.service.pipeline_job_service import start_job, update_job

        job_id = await start_job(
            minute_token, job_type="DOWNLOAD", stage="START", max_attempts=max(1, ready_retries)
        )
        attempts = max(1, ready_retries)
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                await update_job(job_id, stage="DOWNLOADING", percent=10.0)
                await self._download_and_store(
                    record_id,
                    minute_token,
                    unique_key,
                    event_id,
                    fetch_media=fetch_media,
                    fetch_transcript=fetch_transcript,
                )
                download_progress_store.complete(minute_token)
                await update_job(job_id, status="COMPLETED", stage="DONE", percent=100.0, finished=True)
                self._spawn_auto_summary(minute_token)
                self._spawn_r2_share_video(minute_token)
                return DownloadResult(
                    minute_token=minute_token, record_id=record_id, status="COMPLETED"
                )
            except Exception as exc:
                last_error = exc
                err = parse_feishu_api_error(exc)
                # 生成事件经常比转写早几分钟；只有未就绪才值得原地重试，别把权限错误拖成几分钟假等待
                if err.code == MINUTE_NOT_READY_CODE and attempt < attempts:
                    wait = max(0.0, ready_retry_seconds)
                    logger.warning(
                        "妙记尚未转写完成 token=%s，%ds 后进行第 %d/%d 次重试",
                        minute_token,
                        int(wait),
                        attempt + 1,
                        attempts,
                    )
                    current = download_progress_store.get(minute_token)
                    download_progress_store.update(
                        minute_token,
                        percent=max(5, current.percent if current else 5),
                        stage=f"妙记尚未就绪，{int(wait)}s 后重试（{attempt}/{attempts}）",
                    )
                    if wait > 0:
                        await asyncio.sleep(wait)
                    continue
                logger.exception(
                    "下载妙记资源失败 token=%s，怀疑权限不足或妙记尚未转写完成",
                    minute_token,
                )
                break

        assert last_error is not None
        err = parse_feishu_api_error(last_error)
        download_progress_store.fail(minute_token, err.message)
        await update_job(
            job_id,
            status="FAILED",
            stage="FAILED",
            error_message=err.message,
            finished=True,
        )
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            await uow.meeting_records.update(
                MeetingRecordUpdateEntity(
                    id=record_id,
                    status="FAILED",
                    error_message=err.message,
                )
            )
            await uow.commit()
        return DownloadResult(
            minute_token=minute_token,
            record_id=record_id,
            status="FAILED",
            error_message=err.message,
            error_code=err.code,
            needs_app_scope=err.needs_app_scope,
            scope_auth_url=err.scope_auth_url,
            needs_user_login=err.needs_user_login,
        )

    async def download_many(
        self,
        minute_tokens: list[str],
        *,
        skip_if_completed: bool = True,
        redownload_media: bool = False,
        redownload_transcript: bool = False,
    ) -> list[DownloadResult]:
        tokens = [token for token in minute_tokens if token]
        if not tokens:
            return []
        # 池内限流，这里可以一起挂起；结果顺序与入参一致
        return list(
            await asyncio.gather(
                *(
                    self.download_minute(
                        token,
                        skip_if_completed=skip_if_completed,
                        redownload_media=redownload_media,
                        redownload_transcript=redownload_transcript,
                    )
                    for token in tokens
                )
            )
        )

    async def _download_and_store(
        self,
        record_id: int,
        minute_token: str,
        unique_key: str | None,
        feishu_event_id: str,
        *,
        fetch_media: bool = True,
        fetch_transcript: bool = True,
    ) -> None:
        progress = download_progress_store.update

        progress(minute_token, percent=5, stage="获取妙记信息")
        minute_info = await self._minutes.get_minute_info(minute_token, use_user_token=True)
        layout = self._storage.ensure_layout(minute_token)
        existing_meta = self._storage.read_meta(minute_token) or {}

        media_path = existing_meta.get("files", {}).get("media")
        has_video = bool(existing_meta.get("has_video"))
        if fetch_media:
            progress(minute_token, percent=15, stage="获取媒体下载链接")
            media_url = await self._minutes.get_media_download_url(
                minute_token, use_user_token=True
            )
            progress(minute_token, percent=25, stage="下载音视频")
            self._storage.clear_media(minute_token)
            media_path_obj, has_video = await self._storage.save_media(
                minute_token,
                media_url,
                self._minutes._api,
            )
            media_path = str(media_path_obj)
        else:
            progress(minute_token, percent=40, stage="保留已有音视频")

        transcript_path = existing_meta.get("files", {}).get("transcript")
        if fetch_transcript:
            progress(minute_token, percent=75, stage="导出转写文本")
            transcript_bytes = await self._minutes.download_transcript(
                minute_token, use_user_token=True
            )
            self._storage.clear_transcript(minute_token)
            transcript_path = str(
                self._storage.save_transcript(minute_token, transcript_bytes)
            )
        else:
            progress(minute_token, percent=80, stage="保留已有转写")

        progress(minute_token, percent=90, stage="写入本地元数据")

        meta = {
            **existing_meta,
            "minute_token": minute_token,
            "title": minute_info.title,
            "duration_ms": minute_info.duration_ms,
            "has_video": has_video,
            "feishu_event_id": feishu_event_id,
            "unique_key": unique_key,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "files": {
                "media": media_path,
                "transcript": transcript_path,
            },
            "reserved_dirs": {
                "agent": str(layout["agent"]),
                "llm": str(layout["llm"]),
                "output": str(layout["output"]),
            },
        }
        self._storage.write_meta(minute_token, meta)

        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            await uow.meeting_records.update(
                MeetingRecordUpdateEntity(
                    id=record_id,
                    title=minute_info.title,
                    duration_ms=minute_info.duration_ms,
                    has_video=has_video,
                    status="COMPLETED",
                    storage_path=str(layout["base"]),
                    media_relpath=str(media_path) if media_path else None,
                    transcript_relpath=str(transcript_path) if transcript_path else None,
                    storage_root_relpath=minute_token,
                )
            )
            await uow.commit()

        logger.info(
            "妙记资源下载完成 token=%s has_video=%s media=%s transcript=%s path=%s",
            minute_token,
            has_video,
            fetch_media,
            fetch_transcript,
            layout["base"],
        )
