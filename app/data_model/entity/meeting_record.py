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


@dataclass
class MeetingRecordQueryEntity:
    id: int | None = None
    feishu_event_id: str | None = None
    minute_token: str | None = None
    unique_key: str | None = None
    status: str | None = None
