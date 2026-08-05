from dataclasses import dataclass


@dataclass
class ShareEntity:
    id: int | None
    share_token: str
    minute_token: str
    access_mode: str
    allow_export: bool
    access_key_id: int | None
    created_at: int
    revoked_at: int | None


@dataclass
class ShareCreateEntity:
    share_token: str
    minute_token: str
    access_mode: str
    allow_export: bool
    access_key_id: int | None
    created_at: int


@dataclass
class ShareQueryEntity:
    id: int | None = None
    share_token: str | None = None
    minute_token: str | None = None
    include_revoked: bool = False


@dataclass
class ShareAdminQueryEntity:
    """管理端分享列表筛选。status: ACTIVE / REVOKED / ALL。"""

    status: str = "ACTIVE"
    minute_token: str | None = None
    q: str | None = None
    access_key_id: int | None = None
    no_access_key: bool = False
    allow_export: bool | None = None
    access_mode: str | None = None
    created_from: int | None = None
    created_to: int | None = None
    sort_by: str = "CREATED_AT"
    sort_order: str = "DESC"
    limit: int = 200
    offset: int = 0


@dataclass
class ShareBatchEnsureEntity:
    minute_tokens: list[str]
    access_mode: str
    allow_export: bool
    access_key_id: int | None = None


@dataclass
class ShareBatchItemEntity:
    minute_token: str
    share: ShareEntity | None = None
    reused: bool = False
    error: str | None = None


@dataclass
class ShareUpdateEntity:
    id: int
    access_mode: str | None = None
    allow_export: bool | None = None
    access_key_id: int | None = None


@dataclass
class ShareBatchUpdateEntity:
    share_ids: list[int]
    access_mode: str | None = None
    allow_export: bool | None = None
    access_key_id: int | None = None


@dataclass
class ShareBatchRevokeEntity:
    share_ids: list[int]


@dataclass
class ShareBatchResultItemEntity:
    id: int
    ok: bool
    error: str | None = None
    share: ShareEntity | None = None


@dataclass
class ShareBatchResultEntity:
    items: list[ShareBatchResultItemEntity]
    success_count: int
    failed_count: int


@dataclass
class ShareAdminItemEntity:
    share: ShareEntity
    title: str | None = None


@dataclass
class ShareAdminListEntity:
    items: list[ShareAdminItemEntity]
    total: int


@dataclass
class ShareLibraryKeyStatusEntity:
    key_prefix: str
    status: str  # VALID / INVALID / EXPIRED / REVOKED
    share_count: int = 0


@dataclass
class ShareLibraryItemEntity:
    share_token: str
    minute_token: str
    title: str
    access_mode: str
    allow_export: bool
    url: str
    matched_key_prefix: str | None = None
    source: str = "KEY"  # KEY / KNOWN_TOKEN


@dataclass
class ShareLibraryResultEntity:
    items: list[ShareLibraryItemEntity]
    keys: list[ShareLibraryKeyStatusEntity]
