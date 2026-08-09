"""管理端鉴权中间件：Bearer JWT 或 access_ticket。"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.jwt_auth import AuthPrincipal, decode_token
from app.service.admin_ticket_store import admin_ticket_store

_PUBLIC_EXACT = {
    "/api/v1/auth/admin/login",
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/super/login",
    "/api/v1/auth/feishu/sso-exchange",
}
_PUBLIC_PREFIXES = (
    "/api/v1/auth/feishu/login",
    "/api/v1/auth/feishu/callback",
    "/api/v1/auth/invites/check",
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


def is_super_admin_token(token: str | None) -> bool:
    expected = settings.resolved_super_admin_token
    if not expected or not token:
        return False
    return secrets.compare_digest(token, expected)


def resolve_request_principal(request: Request) -> AuthPrincipal | None:
    bearer = extract_bearer_token(request)
    if bearer:
        principal = decode_token(bearer)
        if principal is not None:
            return principal
        # 兼容：极早期仍有人把超管口令当 Bearer；拒绝作为业务会话，避免绕过 JWT
    ticket = (request.query_params.get("access_ticket") or "").strip()
    # query 票收窄：仅 GET/HEAD + 白名单前缀，避免泄露后等于完整会话
    return admin_ticket_store.resolve(
        ticket, method=request.method, path=request.url.path
    )


def is_request_authorized(request: Request) -> bool:
    return resolve_request_principal(request) is not None


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        if _is_public(path):
            return await call_next(request)

        if not (settings.jwt_secret or "").strip():
            return JSONResponse(
                status_code=503,
                content={"detail": "未配置 JWT_SECRET，管理端不可用"},
            )

        principal = resolve_request_principal(request)
        if principal is None:
            # query 票被拒时给更明确的提示（方法/路径不在白名单）
            ticket = (request.query_params.get("access_ticket") or "").strip()
            if ticket:
                return JSONResponse(
                    status_code=401,
                    content={
                        "detail": "访问票无效或无权访问该接口（票仅用于媒体/SSE 只读）"
                    },
                )
            return JSONResponse(
                status_code=401,
                content={"detail": "需要管理员登录"},
            )

        # 身份只挂在 request.state，由 Router 显式向下传递；禁止 ContextVar
        request.state.auth = principal
        return await call_next(request)
