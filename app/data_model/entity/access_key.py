from dataclasses import dataclass


@dataclass
class AccessKeyEntity:
    id: int | None
    name: str
    key_hash: str
    key_prefix: str
    expires_at: int
    created_at: int
    revoked_at: int | None


@dataclass
class AccessKeyCreateEntity:
    name: str
    key_hash: str
    key_prefix: str
    expires_at: int
    created_at: int


@dataclass
class AccessKeyQueryEntity:
    id: int | None = None
    include_revoked: bool = False
