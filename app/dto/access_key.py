from pydantic import BaseModel, Field


class AccessKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expires_at: int = Field(description="UTC 毫秒时间戳")


class AccessKeyResponse(BaseModel):
    id: int
    name: str
    key_prefix: str
    expires_at: int
    created_at: int
    revoked_at: int | None = None
    status: str


class AccessKeyCreateResponse(AccessKeyResponse):
    plaintext_key: str


class AccessKeyListResponse(BaseModel):
    items: list[AccessKeyResponse]


class ShareAccessLogResponse(BaseModel):
    id: int
    access_key_id: int
    share_id: int
    minute_token: str
    action: str
    ip: str | None = None
    user_agent: str | None = None
    created_at: int


class ShareAccessLogListResponse(BaseModel):
    items: list[ShareAccessLogResponse]
