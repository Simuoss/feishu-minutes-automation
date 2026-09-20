"""管理端用户 / 超级管理员 JWT 签发与校验。

访问票短、刷新票长。每次刷新会同时换发两张，刷新票本身也会往后推，
这样常用的人不会因为 7 天访问票到期被踢出去。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt

from app.core.config import settings

Role = Literal["USER", "SUPER_ADMIN"]
TokenType = Literal["access", "refresh"]


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


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int


def _secret() -> str:
    secret = (settings.jwt_secret or "").strip()
    if not secret:
        raise RuntimeError("未配置 JWT_SECRET，无法签发或校验登录态")
    return secret


def _access_ttl() -> int:
    return max(60, int(settings.jwt_expire_seconds))


def _refresh_ttl() -> int:
    return max(_access_ttl(), int(settings.jwt_refresh_expire_seconds))


def _encode(payload: dict[str, Any], *, ttl_seconds: int, typ: TokenType) -> str:
    now = datetime.now(timezone.utc)
    body = {
        **payload,
        "typ": typ,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(body, _secret(), algorithm="HS256")


def _user_claims(*, user_id: int, username: str) -> dict[str, Any]:
    return {
        "sub": str(user_id),
        "role": "USER",
        "username": username,
    }


def _super_claims() -> dict[str, Any]:
    return {"sub": "super", "role": "SUPER_ADMIN"}


def issue_user_token(*, user_id: int, username: str) -> str:
    return _encode(
        _user_claims(user_id=user_id, username=username),
        ttl_seconds=_access_ttl(),
        typ="access",
    )


def issue_super_admin_token() -> str:
    return _encode(_super_claims(), ttl_seconds=_access_ttl(), typ="access")


def issue_user_session(*, user_id: int, username: str) -> TokenPair:
    claims = _user_claims(user_id=user_id, username=username)
    access_ttl = _access_ttl()
    refresh_ttl = _refresh_ttl()
    return TokenPair(
        access_token=_encode(claims, ttl_seconds=access_ttl, typ="access"),
        refresh_token=_encode(claims, ttl_seconds=refresh_ttl, typ="refresh"),
        expires_in=access_ttl,
        refresh_expires_in=refresh_ttl,
    )


def issue_super_session() -> TokenPair:
    claims = _super_claims()
    access_ttl = _access_ttl()
    refresh_ttl = _refresh_ttl()
    return TokenPair(
        access_token=_encode(claims, ttl_seconds=access_ttl, typ="access"),
        refresh_token=_encode(claims, ttl_seconds=refresh_ttl, typ="refresh"),
        expires_in=access_ttl,
        refresh_expires_in=refresh_ttl,
    )


def _principal_from_payload(payload: dict[str, Any]) -> AuthPrincipal | None:
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


def _decode_payload(token: str) -> dict[str, Any] | None:
    raw = (token or "").strip()
    if not raw:
        return None
    try:
        return jwt.decode(raw, _secret(), algorithms=["HS256"])
    except (jwt.PyJWTError, RuntimeError):
        return None


def decode_token(token: str) -> AuthPrincipal | None:
    payload = _decode_payload(token)
    if payload is None:
        return None
    # 旧票没有 typ，仍当访问票；刷新票不能当 Bearer
    if str(payload.get("typ") or "access") != "access":
        return None
    return _principal_from_payload(payload)


def decode_refresh_token(token: str) -> AuthPrincipal | None:
    payload = _decode_payload(token)
    if payload is None:
        return None
    if str(payload.get("typ") or "") != "refresh":
        return None
    return _principal_from_payload(payload)
