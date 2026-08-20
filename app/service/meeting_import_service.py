"""把本地音视频或文档导入成一场会议，之后走同一条纪要流水线。

不是所有课都在飞书上：手机录的音、别人给的录屏、一份会议纪要 docx，都值得进来
享受同一套自建转写与纪要生成。导入的会议用 `event_type=LOCAL_IMPORT` 标记，
所有飞书专属操作（切回飞书转写、重下原片）都要先认出它并明确拒绝。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.core.resource_key import storage_relpath
from app.data_model.entity.meeting_record import (
    MeetingRecordCreateEntity,
    MeetingRecordUpdateEntity,
)
from app.integrations.video.ffmpeg_client import FfmpegError, ffmpeg_client
from app.repository.uow import UnitOfWork
from app.service.document_transcript import (
    TEXT_EXTENSIONS,
    DocumentParseError,
    parse_document,
)
from app.service.meeting_storage_service import (
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    MeetingStorageService,
)
from app.service.transcript_anchor import extract_unique_speakers

logger = logging.getLogger(__name__)

MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
SUPPORTED_EXTENSIONS = MEDIA_EXTENSIONS | TEXT_EXTENSIONS

# 单次直传上限，与 R2 的单个 PUT 限制对齐
MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024

_UNSAFE_NAME_RE = re.compile(r"[^\w.\-（）()\[\]【】\u4e00-\u9fff]+")


class MeetingImportError(RuntimeError):
    """导入失败，消息直接给前端看。"""


@dataclass
class ImportResult:
    minute_token: str
    record_id: int
    title: str
    kind: str
    duration_ms: int | None
    has_video: bool


def new_import_token() -> str:
    """导入会议的 token。

    `imp` 前缀让它一眼区别于飞书的 `ob...`，长度 23 字符也塞得进各表的
    VARCHAR(32)；资源键只要求 [A-Za-z0-9_-]，不要求飞书那种前缀。
    """
    return f"imp{uuid4().hex[:20]}"


def sanitize_filename(name: str) -> str:
    """只留文件名本体，去掉路径与危险字符。"""
    base = Path((name or "").replace("\\", "/")).name.strip()
    cleaned = _UNSAFE_NAME_RE.sub("_", base).strip("._")
    return cleaned or "import"


def classify(filename: str) -> str:
    """按扩展名判断这是媒体还是文档。"""
    suffix = Path(filename).suffix.lower()
    if suffix in MEDIA_EXTENSIONS:
        return "media"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    raise MeetingImportError(
        f"不支持的文件类型 {suffix or '（无扩展名）'}；"
        "音视频支持 mp4/mov/mkv/webm/mp3/m4a/wav/aac/ogg，"
        "文档支持 txt/md/srt/docx/pdf"
    )


class MeetingImportService:
    def __init__(self, storage: MeetingStorageService | None = None) -> None:
        self._storage = storage or MeetingStorageService()

    async def import_file(
        self,
        source: Path,
        *,
        owner_user_id: int,
        filename: str,
        title: str | None = None,
        minute_token: str | None = None,
    ) -> ImportResult:
        """把一份已经落到本地的文件收成会议。source 会被移进会议目录。"""
        kind = classify(filename)
        token = minute_token or new_import_token()
        display_title = (title or "").strip() or Path(filename).stem
        layout = self._storage.ensure_layout(token, owner_user_id=owner_user_id)

        if kind == "media":
            media_path = layout["media"] / sanitize_filename(filename)
            source.replace(media_path)
            duration_ms, has_video = await self._probe_media(media_path)
            transcript_path: Path | None = None
            transcript_source = None
            speakers: list[str] = []
        else:
            try:
                parsed = await self._parse_text(source, filename)
            finally:
                source.unlink(missing_ok=True)
            transcript_path = self._storage.save_transcript(
                token,
                parsed.transcript.encode("utf-8"),
                owner_user_id=owner_user_id,
            )
            media_path = None
            duration_ms = parsed.duration_ms if parsed.exact_duration else None
            has_video = False
            # 纯文本没有音频，覆盖率那套判定对它不成立，必须自报来源
            from app.service.transcription_flow import SOURCE_IMPORT

            transcript_source = SOURCE_IMPORT
            speakers = extract_unique_speakers(parsed.transcript)

        record_id = await self._persist(
            token,
            owner_user_id=owner_user_id,
            title=display_title,
            duration_ms=duration_ms,
            has_video=has_video,
            media_path=media_path,
            transcript_path=transcript_path,
            transcript_source=transcript_source,
            speakers=speakers,
            base_dir=layout["base"],
        )

        from app.service.meeting_download_service import MeetingDownloadService

        await MeetingDownloadService.enqueue_followups(
            token, owner_user_id=owner_user_id
        )
        logger.info(
            "本地导入完成 owner=%s token=%s kind=%s title=%s",
            owner_user_id,
            token,
            kind,
            display_title,
        )
        return ImportResult(
            minute_token=token,
            record_id=record_id,
            title=display_title,
            kind=kind,
            duration_ms=duration_ms,
            has_video=has_video,
        )

    async def _parse_text(self, source: Path, filename: str):
        # 解析要按原始扩展名走，临时文件名可能没有后缀
        suffix = Path(filename).suffix.lower()
        target = source
        if source.suffix.lower() != suffix:
            target = source.with_name(f"{source.name}{suffix}")
            source.replace(target)
        try:
            return parse_document(target)
        except DocumentParseError as exc:
            raise MeetingImportError(str(exc)) from exc
        finally:
            if target != source:
                target.unlink(missing_ok=True)

    @staticmethod
    async def _probe_media(media_path: Path) -> tuple[int | None, bool]:
        try:
            info = await ffmpeg_client.probe(media_path)
        except FfmpegError as exc:
            # 探不出来不该挡住导入：时长只影响展示，转写会自己再探一遍
            logger.warning("导入媒体探测失败 path=%s: %s", media_path, exc)
            return None, media_path.suffix.lower() in VIDEO_EXTENSIONS
        duration_ms = (
            int(info.duration_seconds * 1000) if info.duration_seconds else None
        )
        return duration_ms, info.has_video_stream

    async def _persist(
        self,
        minute_token: str,
        *,
        owner_user_id: int,
        title: str,
        duration_ms: int | None,
        has_video: bool,
        media_path: Path | None,
        transcript_path: Path | None,
        transcript_source: str | None,
        speakers: list[str],
        base_dir: Path,
    ) -> int:
        import json

        from app.service.meeting_download_service import LOCAL_IMPORT_EVENT_TYPE

        now = datetime.now(timezone.utc)
        rel = storage_relpath(owner_user_id, minute_token)
        # 稳定事件键，与手动下载同款写法
        event_id = f"import-{minute_token}:u{owner_user_id}"

        # 先建记录再写 meta：meta 现在也落在 meeting_records 上，反过来会先由
        # META_BACKFILL 占掉这一行，随后建记录就撞 feishu_event_id 的唯一约束
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            record = await uow.meeting_records.create(
                MeetingRecordCreateEntity(
                    feishu_event_id=event_id,
                    event_type=LOCAL_IMPORT_EVENT_TYPE,
                    minute_token=minute_token,
                    # 必须是 COMPLETED，否则 assert_meeting_readable 会让详情全 404
                    status="COMPLETED",
                    storage_path=str(base_dir),
                    owner_user_id=owner_user_id,
                    storage_root_relpath=rel,
                )
            )
            if record.id is None:
                raise MeetingImportError("创建会议记录失败，未返回 record id")
            await uow.meeting_records.update(
                MeetingRecordUpdateEntity(
                    id=record.id,
                    title=title,
                    duration_ms=duration_ms,
                    has_video=has_video,
                    status="COMPLETED",
                    media_relpath=str(media_path) if media_path else None,
                    transcript_relpath=(
                        str(transcript_path) if transcript_path else None
                    ),
                    transcript_source=transcript_source,
                    speakers_json=json.dumps(speakers, ensure_ascii=False),
                    create_time=now.isoformat(),
                )
            )
            await uow.commit()

        await self._storage.write_meta_async(
            minute_token,
            {
                "minute_token": minute_token,
                "title": title,
                "duration_ms": duration_ms,
                "create_time": now.isoformat(),
                "has_video": has_video,
                "feishu_event_id": event_id,
                "downloaded_at": now.isoformat(),
                "status": "COMPLETED",
                "files": {
                    "media": str(media_path) if media_path else None,
                    "transcript": str(transcript_path) if transcript_path else None,
                },
            },
            owner_user_id=owner_user_id,
        )
        return record.id


meeting_import_service = MeetingImportService()
