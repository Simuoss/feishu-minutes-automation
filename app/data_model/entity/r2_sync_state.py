from dataclasses import dataclass, field
from typing import Any


@dataclass
class R2SyncStateEntity:
    id: int | None
    owner_user_id: int
    minute_token: str
    video_key: str | None
    video_etag: str | None
    video_status: str | None
    video_updated_at: str | None
    assets: dict[str, Any]
    updated_at: int


@dataclass
class R2SyncStateUpsertEntity:
    owner_user_id: int
    minute_token: str
    video_key: str | None = None
    video_etag: str | None = None
    video_status: str | None = None
    video_updated_at: str | None = None
    assets: dict[str, Any] = field(default_factory=dict)
    updated_at: int = 0
