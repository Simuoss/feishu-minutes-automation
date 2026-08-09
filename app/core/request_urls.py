"""根据当前请求解析对外 URL（兼容本地 / 公网、直连 7354 / 经 7355 反代）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlparse

from starlette.requests import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

OAUTH_CALLBACK_PATH = "/api/v1/auth/feishu/callback"
_OAUTH_STATE_TTL_SECONDS = 600


def _oauth_state_secret() -> bytes:
    secret = (settings.jwt_secret or "").strip()
    if not secret:
        raise RuntimeError("未配置 JWT_SECRET，无法签发 OAuth state")
    return secret.encode("utf-8")


def request_external_base(request: Request) -> str:
    proto = (
        (request.headers.get("x-forwarded-proto") or request.url.scheme or "http")
        .split(",")[0]
        .strip()
    )
    host = (
        (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc
        )
        .split(",")[0]
        .strip()
    )
    return f"{proto}://{host}".rstrip("/")


def oauth_redirect_allowlist() -> list[str]:
    """飞书后台需登记的全部回调；配置多项时用英文逗号分隔。"""
    raw = (settings.feishu_oauth_redirect_uris or "").strip()
    if raw:
        items = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        primary = (settings.feishu_oauth_redirect_uri or "").strip()
        items = [
            primary,
            "http://127.0.0.1:7355/api/v1/auth/feishu/callback",
            "http://127.0.0.1:7354/api/v1/auth/feishu/callback",
            "http://localhost:7355/api/v1/auth/feishu/callback",
            "http://localhost:7354/api/v1/auth/feishu/callback",
        ]
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def resolve_oauth_redirect_uri(request: Request) -> str:
    candidate = f"{request_external_base(request)}{OAUTH_CALLBACK_PATH}"
    allowed = oauth_redirect_allowlist()
    if candidate in allowed:
        return candidate

    # 127.0.0.1 / localhost 互认
    swapped = candidate
    if "://127.0.0.1" in candidate:
        swapped = candidate.replace("://127.0.0.1", "://localhost", 1)
    elif "://localhost" in candidate:
        swapped = candidate.replace("://localhost", "://127.0.0.1", 1)
    if swapped in allowed:
        return swapped

    fallback = allowed[0] if allowed else candidate
    if fallback != candidate:
        logger.warning(
            "当前请求推导的 OAuth 回调不在白名单，已回退。candidate=%s fallback=%s。"
            "请把本地与公网回调都配进 FEISHU_OAUTH_REDIRECT_URIS，并在飞书开放平台同时登记。",
            candidate,
            fallback,
        )
    return fallback


def frontend_origin_for_redirect_uri(redirect_uri: str) -> str:
    """OAuth 完成后回跳的前端 origin：同域反代直接用回调主机；直连 API 端口则改前端端口。"""
    parsed = urlparse(redirect_uri)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port

    if port == settings.app_port:
        return f"{scheme}://{host}:{settings.frontend_port}"
    if port:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def encode_oauth_state(
    *,
    redirect_uri: str,
    frontend_origin: str,
    reauth: bool = False,
    user_id: int | None = None,
    intent: str = "BIND",
    invite: str | None = None,
) -> str:
    """HMAC 签名的 OAuth state，防止伪造 uid 劫持飞书 Token 绑定。"""
    intent_norm = (intent or "BIND").strip().upper()
    if intent_norm not in {"LOGIN", "BIND"}:
        intent_norm = "BIND"
    payload = {
        "r": redirect_uri,
        "f": frontend_origin,
        "reauth": bool(reauth),
        "intent": intent_norm,
        "exp": int(time.time()) + _OAUTH_STATE_TTL_SECONDS,
    }
    if user_id is not None:
        payload["uid"] = int(user_id)
    if invite:
        payload["invite"] = str(invite).strip()
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(_oauth_state_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def decode_oauth_state(state: str | None) -> dict:
    if not state or "." not in state:
        return {}
    body, sig = state.rsplit(".", 1)
    if not body or not sig:
        return {}
    try:
        expected = hmac.new(
            _oauth_state_secret(), body.encode("ascii"), hashlib.sha256
        ).hexdigest()
    except RuntimeError:
        logger.error("解码 OAuth state 失败：JWT_SECRET 未配置，拒绝信任回调 state")
        return {}
    if not hmac.compare_digest(expected, sig):
        logger.error("OAuth state 签名校验失败，怀疑伪造回调 uid 试图绑飞书 Token")
        return {}
    try:
        pad = "=" * ((4 - len(body) % 4) % 4)
        raw = base64.urlsafe_b64decode(body.encode("ascii") + pad.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    try:
        exp = int(data.get("exp") or 0)
    except (TypeError, ValueError):
        return {}
    if exp < int(time.time()):
        logger.warning("OAuth state 已过期，需重新发起飞书授权")
        return {}
    return data
