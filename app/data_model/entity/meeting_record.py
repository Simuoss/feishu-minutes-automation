from dataclasses import dataclass
from datetime import datetime

from app.data_model.unset import UNSET, Maybe


@dataclass
class MeetingRecordEntity:
    id: int | None
    feishu_event_id: str
    event_type: str
    unique_key: str | None
    minute_token: str | None
    meeting_id: str | None
    title: str | None
    duration_ms: int | None
    has_video: bool | None
    status: str
    error_message: str | None
    storage_path: str | None
    owner_user_id: int | None
    summary_status: str | None
    media_relpath: str | None
    transcript_relpath: str | None
    downloaded_at: int | None
    storage_root_relpath: str | None
    speakers_json: str | None
    create_time: str | None
    transcript_source: str | None
    transcript_coverage: float | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass
class MeetingRecordCreateEntity:
    feishu_event_id: str
    event_type: str
    unique_key: str | None = None
    minute_token: str | None = None
    meeting_id: str | None = None
    title: str | None = None
    duration_ms: int | None = None
    has_video: bool | None = None
    status: str = "PENDING"
    error_message: str | None = None
    storage_path: str | None = None
    owner_user_id: int | None = None
    summary_status: str | None = None
    media_relpath: str | None = None
    transcript_relpath: str | None = None
    downloaded_at: int | None = None
    storage_root_relpath: str | None = None
    speakers_json: str | None = None
    create_time: str | None = None
    transcript_source: str | None = None
    transcript_coverage: float | None = None


@dataclass
class MeetingRecordUpdateEntity:
    """没填的字段保持原样，显式填 None 才是置空。"""

    id: int
    minute_token: Maybe[str] = UNSET
    meeting_id: Maybe[str] = UNSET
    title: Maybe[str] = UNSET
    duration_ms: Maybe[int] = UNSET
    has_video: Maybe[bool] = UNSET
    status: Maybe[str] = UNSET
    error_message: Maybe[str] = UNSET
    storage_path: Maybe[str] = UNSET
    owner_user_id: Maybe[int] = UNSET
    summary_status: Maybe[str] = UNSET
    media_relpath: Maybe[str] = UNSET
    transcript_relpath: Maybe[str] = UNSET
    downloaded_at: Maybe[int] = UNSET
    storage_root_relpath: Maybe[str] = UNSET
    unique_key: Maybe[str] = UNSET
    speakers_json: Maybe[str] = UNSET
    event_type: Maybe[str] = UNSET
    feishu_event_id: Maybe[str] = UNSET
    create_time: Maybe[str] = UNSET
    transcript_source: Maybe[str] = UNSET
    transcript_coverage: Maybe[float] = UNSET


@dataclass
class MeetingRecordQueryEntity:
    id: int | None = None
    feishu_event_id: str | None = None
    minute_token: str | None = None
    unique_key: str | None = None
    status: str | None = None
    summary_status: str | None = None
    owner_user_id: int | None = None
