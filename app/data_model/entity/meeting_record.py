from dataclasses import dataclass
from datetime import datetime


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
    id: int
    minute_token: str | None = None
    meeting_id: str | None = None
    title: str | None = None
    duration_ms: int | None = None
    has_video: bool | None = None
    status: str | None = None
    error_message: str | None = None
    storage_path: str | None = None
    owner_user_id: int | None = None
    summary_status: str | None = None
    media_relpath: str | None = None
    transcript_relpath: str | None = None
    downloaded_at: int | None = None
    storage_root_relpath: str | None = None
    unique_key: str | None = None
    speakers_json: str | None = None
    event_type: str | None = None
    feishu_event_id: str | None = None
    create_time: str | None = None
    transcript_source: str | None = None
    transcript_coverage: float | None = None


@dataclass
class MeetingRecordQueryEntity:
    id: int | None = None
    feishu_event_id: str | None = None
    minute_token: str | None = None
    unique_key: str | None = None
    status: str | None = None
    summary_status: str | None = None
    owner_user_id: int | None = None
