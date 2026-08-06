"""多租户资源主键：(owner_user_id, minute_token)。"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def require_owner_user_id(owner_user_id: int | None) -> int:
    if owner_user_id is None:
        raise ValueError("缺少 owner_user_id，拒绝访问无归属资源")
    return int(owner_user_id)


def normalize_minute_token(minute_token: str) -> str:
    token = (minute_token or "").strip()
    if not token:
        raise ValueError("缺少 minute_token")
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("minute_token 含非法字符，拒绝路径拼接")
    return token


def resource_key(owner_user_id: int, minute_token: str) -> str:
    """进程内 dict/池用的稳定字符串键。"""
    uid = require_owner_user_id(owner_user_id)
    token = normalize_minute_token(minute_token)
    return f"{uid}:{token}"


def storage_relpath(owner_user_id: int, minute_token: str) -> str:
    uid = require_owner_user_id(owner_user_id)
    token = normalize_minute_token(minute_token)
    return f"{uid}/{token}"
