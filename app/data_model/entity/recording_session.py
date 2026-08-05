from dataclasses import dataclass
from datetime import datetime


@dataclass
class RecordingSessionEntity:
    id: int | None
    unique_key: str
    minute_token: str | None
    meeting_id: str | None
    object_type: str | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass
class RecordingSessionCreateEntity:
    unique_key: str
    minute_token: str | None = None
    meeting_id: str | None = None
    object_type: str | None = None


@dataclass
class RecordingSessionUpdateEntity:
    id: int
    minute_token: str | None = None
    meeting_id: str | None = None
    object_type: str | None = None
