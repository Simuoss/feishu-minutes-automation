"""按会议来源与媒体类型选出进度条该怎么画。

五种用户能对上的场景：飞书全转写、飞书不全转写、上传视频、上传音频、上传文字。
飞书音频会议没有画面，沿用全/不全两个 id，只把 has_figures 关掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.service.meeting_download_service import LOCAL_IMPORT_EVENT_TYPE
from app.service.meeting_storage_service import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from app.service.transcription_flow import SOURCE_ASR, SOURCE_IMPORT


@dataclass(frozen=True)
class ProgressProfile:
    id: str
    label: str
    has_transcribe: bool
    has_figures: bool
    has_share: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "has_transcribe": self.has_transcribe,
            "has_figures": self.has_figures,
            "has_share": self.has_share,
        }


PROFILE_FEISHU_FULL = ProgressProfile(
    "feishu_full", "飞书全转写", False, True, True
)
PROFILE_FEISHU_PARTIAL = ProgressProfile(
    "feishu_partial", "飞书不全转写", True, True, True
)
PROFILE_IMPORT_VIDEO = ProgressProfile(
    "import_video", "上传视频", True, True, True
)
PROFILE_IMPORT_AUDIO = ProgressProfile(
    "import_audio", "上传音频", True, False, False
)
PROFILE_IMPORT_TEXT = ProgressProfile(
    "import_text", "上传文字", False, False, False
)


def detect_media_kind(
    *,
    video_path: Path | None,
    media_path: Path | None,
    has_video: bool | None,
    event_type: str | None,
    transcript_source: str | None,
) -> str:
    """本地文件优先，其次看记录上的 has_video。"""
    if video_path is not None:
        return "video"
    if media_path is not None:
        suffix = media_path.suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            return "video"
        if suffix in AUDIO_EXTENSIONS:
            return "audio"
    if has_video:
        return "video"
    is_import = (event_type or "") == LOCAL_IMPORT_EVENT_TYPE
    if is_import:
        if transcript_source == SOURCE_IMPORT or media_path is None:
            return "text"
        return "audio"
    if has_video is False:
        return "audio"
    return "video"


def classify_progress_profile(
    *,
    event_type: str | None,
    transcript_source: str | None,
    media_kind: str,
    transcribe_inflight: bool = False,
) -> ProgressProfile:
    is_import = (event_type or "") == LOCAL_IMPORT_EVENT_TYPE
    needs_asr = transcribe_inflight or transcript_source == SOURCE_ASR

    if is_import:
        if media_kind == "text":
            return PROFILE_IMPORT_TEXT
        if media_kind == "audio":
            return PROFILE_IMPORT_AUDIO
        return PROFILE_IMPORT_VIDEO

    has_figures = media_kind == "video"
    if needs_asr:
        return (
            PROFILE_FEISHU_PARTIAL
            if has_figures
            else ProgressProfile("feishu_partial", "飞书不全转写", True, False)
        )
    return (
        PROFILE_FEISHU_FULL
        if has_figures
        else ProgressProfile("feishu_full", "飞书全转写", False, False)
    )


async def resolve_progress_profile(
    minute_token: str,
    *,
    owner_user_id: int,
    transcribe_inflight: bool = False,
    storage: Any | None = None,
) -> ProgressProfile:
    from app.repository.uow import UnitOfWork
    from app.service.meeting_storage_service import MeetingStorageService

    store = storage or MeetingStorageService()
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        record = await uow.meeting_records.get_latest_by_minute_token(
            minute_token, owner_user_id=owner_user_id
        )
    video_path = store.find_video_path(minute_token, owner_user_id=owner_user_id)
    media_path = store.find_media_path(minute_token, owner_user_id=owner_user_id)
    kind = detect_media_kind(
        video_path=video_path,
        media_path=media_path,
        has_video=record.has_video if record else None,
        event_type=record.event_type if record else None,
        transcript_source=record.transcript_source if record else None,
    )
    return classify_progress_profile(
        event_type=record.event_type if record else None,
        transcript_source=record.transcript_source if record else None,
        media_kind=kind,
        transcribe_inflight=transcribe_inflight,
    )
