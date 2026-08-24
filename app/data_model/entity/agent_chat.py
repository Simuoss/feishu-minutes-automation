from dataclasses import dataclass, field
from typing import Any

# 提问主体：登录用户 / 持密钥访客 / 公开访客
SUBJECT_USER = "USER"
SUBJECT_ACCESS_KEY = "ACCESS_KEY"
SUBJECT_ANON = "ANON"

# 检索范围：本人名下 / 全站（超管）/ 分享可见集合
SCOPE_OWN = "OWN"
SCOPE_ALL = "ALL"
SCOPE_SHARE = "SHARE"

ROLE_USER = "USER"
ROLE_ASSISTANT = "ASSISTANT"
ROLE_CARD = "CARD"
ROLE_TOOL = "TOOL"


@dataclass
class AgentSessionEntity:
    id: int | None
    sdk_session_id: str
    subject_type: str
    subject_key: str
    owner_user_id: int | None
    scope: str
    title: str
    turn_count: int
    created_at: int
    updated_at: int


@dataclass
class AgentSessionCreateEntity:
    sdk_session_id: str
    subject_type: str
    subject_key: str
    owner_user_id: int | None
    scope: str
    created_at: int
    updated_at: int
    title: str = ""


@dataclass
class AgentMessageEntity:
    id: int | None
    session_id: int
    seq: int
    role: str
    content: str
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: int = 0


@dataclass
class AgentMessageCreateEntity:
    session_id: int
    role: str
    content: str
    created_at: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentUsageEntity:
    subject_type: str
    subject_key: str
    day: str
    used: int
