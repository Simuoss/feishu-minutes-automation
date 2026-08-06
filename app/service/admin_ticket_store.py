"""管理端短期访问票：给 EventSource / <video src> 等无法带 Header 的请求用。

票绑定 JWT 主体，且仅授权只读 GET/HEAD + 白名单路径前缀。
勿把长期口令或全量写权限放进 URL。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from app.core.jwt_auth import AuthPrincipal, Role
from app.service.time_utils import utc_now_ms

# 媒体/SSE 场景够用；前端 ensureAccessTicket 会在过期前刷新
TICKET_TTL_MS = 30 * 60 * 1000

TICKET_ALLOWED_METHODS = frozenset({"GET", "HEAD"})
# query 票不得打管理写接口、下载入队、改分享等
TICKET_ALLOWED_PREFIXES = (
    "/api/v1/meetings/",
    "/api/v1/auth/feishu/",
)


@dataclass
class AdminTicket:
    ticket: str
    expires_at: int
    role: Role
    user_id: int | None
    username: str | None = None


class AdminTicketStore:
    def __init__(self) -> None:
        self._tickets: dict[str, AdminTicket] = {}

    def issue(self, principal: AuthPrincipal) -> AdminTicket:
        self._purge()
        item = AdminTicket(
            ticket=secrets.token_urlsafe(24),
            expires_at=utc_now_ms() + TICKET_TTL_MS,
            role=principal.role,
            user_id=principal.user_id,
            username=principal.username,
        )
        self._tickets[item.ticket] = item
        return item

    def resolve(
        self,
        ticket: str | None,
        *,
        method: str | None = None,
        path: str | None = None,
    ) -> AuthPrincipal | None:
        if not ticket:
            return None
        self._purge()
        item = self._tickets.get(ticket)
        if item is None:
            return None
        if item.expires_at <= utc_now_ms():
            self._tickets.pop(ticket, None)
            return None

        if method is not None and method.upper() not in TICKET_ALLOWED_METHODS:
            return None
        if path is not None and not any(
            path.startswith(prefix) for prefix in TICKET_ALLOWED_PREFIXES
        ):
            return None

        return AuthPrincipal(
            role=item.role,
            user_id=item.user_id,
            username=item.username,
        )

    def valid(
        self,
        ticket: str | None,
        *,
        method: str | None = None,
        path: str | None = None,
    ) -> bool:
        return self.resolve(ticket, method=method, path=path) is not None

    def _purge(self) -> None:
        now = utc_now_ms()
        dead = [k for k, v in self._tickets.items() if v.expires_at <= now]
        for key in dead:
            self._tickets.pop(key, None)


admin_ticket_store = AdminTicketStore()
