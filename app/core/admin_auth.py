"""管理端鉴权中间件。

- 常规 API：Authorization: Bearer <ADMIN_TOKEN>
- 登录：POST body { token }
- EventSource / <video src> 无法带 Header：使用短期 access_ticket 查询参数
  （由已登录管理员兑换，不是 ADMIN_TOKEN 本身）
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.service.admin_ticket_store import admin_ticket_store

# 飞书 OAuth 是浏览器整页跳转，无法带 Bearer；分享访客走 /share/*
_PUBLIC_EXACT = {
    "/api/v1/auth/admin/login",
}
_PUBLIC_PREFIXES = (
    "/api/v1/auth/feishu/login",
    "/api/v1/auth/feishu/callback",
    "/api/v1/share/",
)


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def extract_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    return None


def is_admin_token(token: str | None) -> bool:
    expected = (settings.admin_token or "").strip()
    if not expected or not token:
        return False
    return secrets.compare_digest(token, expected)


def is_request_authorized(request: Request) -> bool:
    if is_admin_token(extract_bearer_token(request)):
        return True
    # 兼容旧参数名时直接拒绝：不再接受 admin_token 进 URL
    ticket = (request.query_params.get("access_ticket") or "").strip()
    return admin_ticket_store.valid(ticket)


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        if _is_public(path):
            return await call_next(request)

        if not (settings.admin_token or "").strip():
            return JSONResponse(
                status_code=503,
                content={"detail": "未配置 ADMIN_TOKEN，管理端不可用"},
            )

        if not is_request_authorized(request):
            return JSONResponse(
                status_code=401,
                content={"detail": "需要管理员登录"},
            )
        return await call_next(request)
