from datetime import datetime

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str = "ok"


class MeetingRecordResponse(BaseModel):
    id: int
    feishu_event_id: str
    event_type: str
    unique_key: str | None = None
    minute_token: str | None = None
    meeting_id: str | None = None
    title: str | None = None
    duration_ms: int | None = None
    has_video: bool | None = None
    status: str
    error_message: str | None = None
    storage_path: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
