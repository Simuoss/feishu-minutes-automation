"""检索助手的请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AgentSessionData(BaseModel):
    id: int
    title: str = ""
    scope: str
    turn_count: int = 0
    created_at: int
    updated_at: int


class AgentSessionListResponse(BaseModel):
    items: list[AgentSessionData] = Field(default_factory=list)
    quota_limit: int = 0
    quota_used: int = 0


class AgentMessageData(BaseModel):
    seq: int
    role: str
    content: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: int


class AgentMessageListResponse(BaseModel):
    items: list[AgentMessageData] = Field(default_factory=list)


class AgentChatRequest(BaseModel):
    question: str = ""


class GuestScopeMixin(BaseModel):
    """访客身份：本机保存的密钥明文 + 已知的公开分享 token，与 /share/library 一致。"""

    keys: list[str] = Field(default_factory=list, max_length=30)
    share_tokens: list[str] = Field(default_factory=list, max_length=100)


class GuestSessionRequest(GuestScopeMixin):
    pass


class GuestChatRequest(GuestScopeMixin):
    question: str = ""


class AgentStatusResponse(BaseModel):
    enabled: bool
    quota_limit: int = 0
    quota_used: int = 0
    file_count: int | None = None
