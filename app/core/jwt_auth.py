"""管理端用户 / 超级管理员 JWT 签发与校验。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt

from app.core.config import settings

Role = Literal["USER", "SUPER_ADMIN"]


@dataclass(frozen=True)
class AuthPrincipal:
    role: Role
    user_id: int | None = None
    username: str | None = None

    @property
    def is_super_admin(self) -> bool:
        return self.role == "SUPER_ADMIN"

    @property
    def is_user(self) -> bool:
        return self.role == "USER" and self.user_id is not None


def _secret() -> str:
    secret = (settings.jwt_secret or "").strip()
    if not secret:
        raise RuntimeError("未配置 JWT_SECRET，无法签发或校验登录态")
    return secret


def issue_user_token(*, user_id: int, username: str) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": "USER",
        "username": username,
        "iat": now,
        "exp": now + timedelta(seconds=max(60, int(settings.jwt_expire_seconds))),
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def issue_super_admin_token() -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": "super",
        "role": "SUPER_ADMIN",
        "iat": now,
        "exp": now + timedelta(seconds=max(60, int(settings.jwt_expire_seconds))),
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_token(token: str) -> AuthPrincipal | None:
    raw = (token or "").strip()
    if not raw:
        return None
    try:
        payload = jwt.decode(raw, _secret(), algorithms=["HS256"])
    except (jwt.PyJWTError, RuntimeError):
        return None
    role = str(payload.get("role") or "").upper()
    if role == "SUPER_ADMIN":
        return AuthPrincipal(role="SUPER_ADMIN")
    if role != "USER":
        return None
    sub = payload.get("sub")
    try:
        user_id = int(sub)
    except (TypeError, ValueError):
        return None
    username = payload.get("username")
    return AuthPrincipal(
        role="USER",
        user_id=user_id,
        username=str(username) if username else None,
    )
