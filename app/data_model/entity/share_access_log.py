from dataclasses import dataclass


@dataclass
class ShareAccessLogEntity:
    id: int | None
    access_key_id: int
    share_id: int
    minute_token: str
    action: str
    ip: str | None
    user_agent: str | None
    created_at: int


@dataclass
class ShareAccessLogCreateEntity:
    access_key_id: int
    share_id: int
    minute_token: str
    action: str
    ip: str | None
    user_agent: str | None
    created_at: int
