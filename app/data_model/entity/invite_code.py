from dataclasses import dataclass


@dataclass
class InviteCodeEntity:
    id: int | None
    code: str
    created_by_user_id: int
    created_at: int
    used_by_user_id: int | None
    used_at: int | None
    status: str


@dataclass
class InviteCodeCreateEntity:
    code: str
    created_by_user_id: int
    created_at: int
    status: str = "ACTIVE"


@dataclass
class InviteCodeQueryEntity:
    created_by_user_id: int | None = None
    status: str | None = None
    code: str | None = None
