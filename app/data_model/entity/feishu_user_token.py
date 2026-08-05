from dataclasses import dataclass


@dataclass
class FeishuUserTokenEntity:
    id: int | None
    user_id: int
    access_token: str | None
    refresh_token: str | None
    expires_at: int | None
    refresh_expires_at: int | None
    scope: str | None
    updated_at: int


@dataclass
class FeishuUserTokenUpsertEntity:
    user_id: int
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: int | None = None
    refresh_expires_at: int | None = None
    scope: str | None = None
    updated_at: int = 0
