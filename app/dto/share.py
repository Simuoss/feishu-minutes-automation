from pydantic import BaseModel, Field


class ShareCreateRequest(BaseModel):
    access_mode: str = Field(description="PUBLIC 或 KEY_REQUIRED")
    allow_export: bool = False
    access_key_id: int | None = None


class ShareUpdateRequest(BaseModel):
    access_mode: str | None = Field(default=None, description="PUBLIC 或 KEY_REQUIRED")
    allow_export: bool | None = None
    access_key_id: int | None = None


class ShareBatchCreateRequest(BaseModel):
    minute_tokens: list[str] = Field(min_length=1)
    access_mode: str = Field(description="PUBLIC 或 KEY_REQUIRED")
    allow_export: bool = False
    access_key_id: int | None = None


class ShareBatchUpdateRequest(BaseModel):
    share_ids: list[int] = Field(min_length=1)
    access_mode: str | None = Field(default=None, description="PUBLIC 或 KEY_REQUIRED")
    allow_export: bool | None = None
    access_key_id: int | None = None


class ShareBatchRevokeRequest(BaseModel):
    share_ids: list[int] = Field(min_length=1)


class ShareResponse(BaseModel):
    id: int
    share_token: str
    minute_token: str
    access_mode: str
    allow_export: bool
    access_key_id: int | None = None
    created_at: int
    revoked_at: int | None = None
    url: str
    title: str | None = None


class ShareListResponse(BaseModel):
    items: list[ShareResponse]
    total: int | None = None


class ShareBatchItemResponse(BaseModel):
    minute_token: str
    url: str | None = None
    reused: bool = False
    error: str | None = None


class ShareBatchCreateResponse(BaseModel):
    items: list[ShareBatchItemResponse]


class ShareBatchResultItemResponse(BaseModel):
    id: int
    ok: bool
    error: str | None = None
    share: ShareResponse | None = None


class ShareBatchResultResponse(BaseModel):
    items: list[ShareBatchResultItemResponse]
    success_count: int
    failed_count: int


class ShareMetaResponse(BaseModel):
    share_token: str
    minute_token: str
    title: str
    access_mode: str
    requires_key: bool
    allow_export: bool


class ShareUnlockRequest(BaseModel):
    key: str = Field(min_length=1)


class ShareTryKeysRequest(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=30)


class ShareUnlockResponse(BaseModel):
    share_session: str
    allow_export: bool
    minute_token: str
    matched_key_prefix: str | None = None


class ShareLibraryRequest(BaseModel):
    keys: list[str] = Field(default_factory=list, max_length=30)
    share_tokens: list[str] = Field(default_factory=list, max_length=100)


class ShareLibraryKeyStatusData(BaseModel):
    key_prefix: str
    status: str
    share_count: int = 0


class ShareLibraryItemData(BaseModel):
    share_token: str
    minute_token: str
    title: str
    access_mode: str
    allow_export: bool
    url: str
    matched_key_prefix: str | None = None
    source: str
    duration_ms: int | None = None
    create_time: str | None = None


class ShareLibraryResponse(BaseModel):
    items: list[ShareLibraryItemData]
    keys: list[ShareLibraryKeyStatusData]


class ShareTrackRequest(BaseModel):
    action: str = Field(description="PLAY_VIDEO 或 SESSION_END")
    session_id: str | None = None
    video_progress_pct: int | None = Field(default=None, ge=0, le=100)
    dwell_ms: int | None = Field(default=None, ge=0)
    started_at: int | None = None
    ended_at: int | None = None
    detail: dict | None = None
