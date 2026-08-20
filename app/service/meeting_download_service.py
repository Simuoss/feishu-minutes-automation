import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core import runtime_config
from app.core.resource_key import storage_relpath
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
from app.service.transcript_anchor import extract_unique_speakers

logger = logging.getLogger(__name__)

MANUAL_EVENT_TYPE = "MANUAL_DOWNLOAD"
# 本地导入的会议：飞书上没有对应妙记，一切飞书专属操作都要先拦住
LOCAL_IMPORT_EVENT_TYPE = "LOCAL_IMPORT"


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
        self._minutes = minutes_client

    def _minutes_for(self, user_id: int) -> FeishuMinutesClient:
        return self._minutes or FeishuMinutesClient(user_id=user_id)

    @staticmethod
    async def enqueue_followups(minute_token: str, *, owner_user_id: int) -> None:
        """下载或本地导入收尾后，按这场会议实际有什么来排后续作业。"""
        from app.service.pipeline_queue import (
            JOB_SHARE_VIDEO,
            JOB_SUMMARY,
            enqueue_job,
        )
        from app.service.r2_media_service import r2_media_service
        from app.service.transcription_flow import (
            enqueue_transcribe,
            evaluate_transcript_coverage,
            has_media_for_asr,
        )
        from app.service.voiceprint_flow import enqueue_voiceprint

        storage = MeetingStorageService()
        has_video = (
            storage.find_video_path(minute_token, owner_user_id=owner_user_id)
            is not None
        )
        if r2_media_service.enabled() and has_video:
            await enqueue_job(
                minute_token,
                owner_user_id=owner_user_id,
                job_type=JOB_SHARE_VIDEO,
            )

        _coverage, needs_asr = await evaluate_transcript_coverage(
            minute_token, owner_user_id=owner_user_id
        )
        if needs_asr and has_media_for_asr(
            minute_token, owner_user_id=owner_user_id
        ):
            # 纪要要读完整转写，所以这里只排转写；转写作业跑完自己会放纪要进队
            await enqueue_transcribe(minute_token, owner_user_id=owner_user_id)
            logger.info(
                "转写不完整，已改走自建转写 owner=%s token=%s",
                owner_user_id,
                minute_token,
            )
            return

        # 沿用飞书转写就意味着段头挂着真名，正好拿来给声纹库认人
        if has_video or storage.find_media_path(
            minute_token, owner_user_id=owner_user_id
        ):
            await enqueue_voiceprint(minute_token, owner_user_id=owner_user_id)

        if not runtime_config.get_bool("SUMMARY_AUTO_GENERATE", True):
            return
        await enqueue_job(
            minute_token,
            owner_user_id=owner_user_id,
            job_type=JOB_SUMMARY,
            mode="FULL",
        )
        logger.info(
            "已为妙记入队自动生成纪要 owner=%s token=%s",
            owner_user_id,
            minute_token,
        )

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
        owner_user_id: int | None = None,
    ) -> DownloadResult:
        if owner_user_id is None:
            logger.error(
                "下载拒绝：缺少归属用户 token=%s，怀疑后台任务未显式传入 owner",
                minute_token,
            )
            return DownloadResult(
                minute_token=minute_token,
                record_id=None,
                status="FAILED",
                error_message="缺少 owner_user_id，拒绝写入无归属数据",
            )
        owner_id = int(owner_user_id)

        fetch_media = redownload_media or not self._storage.has_media(
            minute_token, owner_user_id=owner_id
        )
        fetch_transcript = redownload_transcript or not self._storage.has_transcript(
            minute_token, owner_user_id=owner_id
        )

        if (
            skip_if_completed
            and not redownload_media
            and not redownload_transcript
        ):
            async with UnitOfWork() as uow:
                assert uow.meeting_records is not None
                existing = await uow.meeting_records.get_latest_by_minute_token(
                    minute_token, owner_user_id=owner_id
                )
            if existing and existing.status == "COMPLETED":
                logger.info(
                    "当前用户已完成该妙记，跳过重复下载 token=%s owner=%s",
                    minute_token,
                    owner_id,
                )
                download_progress_store.complete(
                    minute_token, owner_user_id=owner_id
                )
                await self.enqueue_followups(
                    minute_token, owner_user_id=owner_id
                )
                return DownloadResult(
                    minute_token=minute_token,
                    record_id=existing.id,
                    status="COMPLETED",
                )

        if download_pool.is_active(minute_token, owner_user_id=owner_id):
            # 池内单飞：附身已有 future，不会再 clear_media / 开第二条下载流水
            logger.info(
                "下载已在进行中，附身已有任务 token=%s owner=%s",
                minute_token,
                owner_id,
            )
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
                owner_user_id=owner_id,
            ),
            owner_user_id=owner_id,
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
        owner_user_id: int,
    ) -> DownloadResult:
        owner_id = int(owner_user_id)
        rel = storage_relpath(owner_id, minute_token)
        storage_path = str(
            self._storage.get_meeting_dir(minute_token, owner_user_id=owner_id)
        )

        # 稳定事件键：同用户同 token 反复录入必须覆盖，禁止每次 UUID 开新行
        raw_event = (feishu_event_id or f"manual-{minute_token}").strip()
        event_id = (
            raw_event
            if raw_event.endswith(f":u{owner_id}")
            else f"{raw_event}:u{owner_id}"
        )

        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            existing = await uow.meeting_records.get_latest_by_minute_token(
                minute_token, owner_user_id=owner_id
            )
            if existing is not None and existing.id is not None:
                record = await uow.meeting_records.update(
                    MeetingRecordUpdateEntity(
                        id=existing.id,
                        event_type=event_type,
                        unique_key=unique_key or existing.unique_key,
                        minute_token=minute_token,
                        status="DOWNLOADING",
                        error_message="",
                        storage_path=storage_path,
                        owner_user_id=owner_id,
                        storage_root_relpath=rel,
                    )
                )
                if record is None or record.id is None:
                    raise RuntimeError("覆盖下载记录失败，未返回 record id")
                event_id = existing.feishu_event_id
                logger.info(
                    "覆盖已有会议主档重新下载 token=%s owner=%s record_id=%s",
                    minute_token,
                    owner_id,
                    record.id,
                )
            else:
                by_event = await uow.meeting_records.get_by_event_id(event_id)
                if by_event is not None and by_event.id is not None:
                    record = await uow.meeting_records.update(
                        MeetingRecordUpdateEntity(
                            id=by_event.id,
                            event_type=event_type,
                            unique_key=unique_key or by_event.unique_key,
                            minute_token=minute_token,
                            status="DOWNLOADING",
                            error_message="",
                            storage_path=storage_path,
                            owner_user_id=owner_id,
                            storage_root_relpath=rel,
                        )
                    )
                    if record is None or record.id is None:
                        raise RuntimeError("按事件覆盖下载记录失败，未返回 record id")
                else:
                    record = await uow.meeting_records.create(
                        MeetingRecordCreateEntity(
                            feishu_event_id=event_id,
                            event_type=event_type,
                            unique_key=unique_key,
                            minute_token=minute_token,
                            status="DOWNLOADING",
                            storage_path=storage_path,
                            owner_user_id=owner_id,
                            storage_root_relpath=rel,
                        )
                    )
            record_id = record.id
            if record_id is None:
                raise RuntimeError("创建下载记录失败，未返回 record id")
            await uow.commit()

        download_progress_store.start(minute_token, owner_user_id=owner_id)
        from app.service.pipeline_job_service import start_job, update_job

        job_id = await start_job(
            minute_token,
            job_type="DOWNLOAD",
            stage="START",
            max_attempts=max(1, ready_retries),
            owner_user_id=owner_id,
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
                    owner_user_id=owner_id,
                )
                download_progress_store.complete(
                    minute_token, owner_user_id=owner_id
                )
                await update_job(
                    job_id,
                    status="COMPLETED",
                    stage="DONE",
                    percent=100.0,
                    finished=True,
                )
                # 下载完成后即可压缩上云供播放；原片仍保留，待纪要完成后 finalize 再删
                await self.enqueue_followups(
                    minute_token, owner_user_id=owner_id
                )
                return DownloadResult(
                    minute_token=minute_token, record_id=record_id, status="COMPLETED"
                )
            except Exception as exc:
                last_error = exc
                err = parse_feishu_api_error(exc)
                if err.code == MINUTE_NOT_READY_CODE and attempt < attempts:
                    wait = max(0.0, ready_retry_seconds)
                    logger.warning(
                        "妙记尚未转写完成 token=%s，%ds 后进行第 %d/%d 次重试",
                        minute_token,
                        int(wait),
                        attempt + 1,
                        attempts,
                    )
                    current = download_progress_store.get(
                        minute_token, owner_user_id=owner_id
                    )
                    download_progress_store.update(
                        minute_token,
                        owner_user_id=owner_id,
                        percent=max(5, current.percent if current else 5),
                        stage=(
                            f"妙记尚未就绪，{int(wait)}s 后重试"
                            f"（{attempt}/{attempts}）"
                        ),
                    )
                    if wait > 0:
                        await asyncio.sleep(wait)
                    continue
                logger.exception(
                    "下载妙记资源失败 token=%s owner=%s，怀疑权限不足或妙记尚未转写完成",
                    minute_token,
                    owner_id,
                )
                break

        assert last_error is not None
        err = parse_feishu_api_error(last_error)
        download_progress_store.fail(
            minute_token, err.message, owner_user_id=owner_id
        )
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
        owner_user_id: int | None = None,
    ) -> list[DownloadResult]:
        tokens = [token for token in minute_tokens if token]
        if not tokens:
            return []
        return list(
            await asyncio.gather(
                *(
                    self.download_minute(
                        token,
                        skip_if_completed=skip_if_completed,
                        redownload_media=redownload_media,
                        redownload_transcript=redownload_transcript,
                        owner_user_id=owner_user_id,
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
        owner_user_id: int,
    ) -> None:
        owner_id = int(owner_user_id)
        minutes = self._minutes_for(owner_id)

        def progress(*, percent: int, stage: str) -> None:
            download_progress_store.update(
                minute_token,
                owner_user_id=owner_id,
                percent=percent,
                stage=stage,
            )

        progress(percent=5, stage="获取妙记信息")
        minute_info = await minutes.get_minute_info(minute_token, use_user_token=True)
        layout = self._storage.ensure_layout(
            minute_token, owner_user_id=owner_id
        )
        existing_meta = (
            await self._storage.read_meta_async(
                minute_token, owner_user_id=owner_id
            )
            or {}
        )

        media_path = existing_meta.get("files", {}).get("media")
        has_video = bool(existing_meta.get("has_video"))
        if fetch_media:
            progress(percent=15, stage="获取媒体下载链接")
            media_url = await minutes.get_media_download_url(
                minute_token, use_user_token=True
            )
            progress(percent=25, stage="下载音视频")
            self._storage.clear_media(minute_token, owner_user_id=owner_id)
            media_path_obj, has_video = await self._storage.save_media(
                minute_token,
                media_url,
                minutes._api,
                owner_user_id=owner_id,
            )
            media_path = str(media_path_obj)
        else:
            progress(percent=40, stage="保留已有音视频")

        transcript_path = existing_meta.get("files", {}).get("transcript")
        speakers: list[str] = []
        if fetch_transcript:
            progress(percent=75, stage="导出转写文本")
            transcript_bytes = await minutes.download_transcript(
                minute_token, use_user_token=True
            )
            self._storage.clear_transcript(minute_token, owner_user_id=owner_id)
            transcript_path = str(
                self._storage.save_transcript(
                    minute_token, transcript_bytes, owner_user_id=owner_id
                )
            )
            try:
                speakers = extract_unique_speakers(
                    transcript_bytes.decode("utf-8", errors="replace")
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "转写已保存但解析参会人失败 token=%s，列表人员筛选将缺该项。"
                    "怀疑转写格式与飞书段落头不一致。err=%s",
                    minute_token,
                    exc,
                )
        else:
            progress(percent=80, stage="保留已有转写")
            existing_text = await self._storage.read_transcript_async(
                minute_token, owner_user_id=owner_id
            )
            if existing_text:
                speakers = extract_unique_speakers(existing_text)

        progress(percent=90, stage="写入本地元数据")

        meta = {
            **existing_meta,
            "minute_token": minute_token,
            "title": minute_info.title,
            "duration_ms": minute_info.duration_ms,
            # 飞书妙记生成时间（毫秒时间戳或可解析字符串），侧栏/列表按此排序展示
            "create_time": minute_info.create_time or existing_meta.get("create_time"),
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
        await self._storage.write_meta_async(
            minute_token, meta, owner_user_id=owner_id
        )

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
                    storage_root_relpath=storage_relpath(owner_id, minute_token),
                    speakers_json=json.dumps(speakers, ensure_ascii=False),
                    create_time=minute_info.create_time or existing_meta.get("create_time"),
                )
            )
            await uow.commit()

        logger.info(
            "妙记资源下载完成 owner=%s token=%s has_video=%s media=%s transcript=%s path=%s",
            owner_id,
            minute_token,
            has_video,
            fetch_media,
            fetch_transcript,
            layout["base"],
        )
