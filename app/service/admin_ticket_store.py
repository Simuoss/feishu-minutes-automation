"""管理端短期访问票：给 EventSource / <video src> 等无法带 Header 的请求用。

真正的 ADMIN_TOKEN 只应出现在 Authorization 或登录 body，绝不进 URL。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from app.service.time_utils import utc_now_ms

# 媒体播放与 SSE 可能持续较久；票本身可作废，泄露窗口远小于长期口令
TICKET_TTL_MS = 2 * 60 * 60 * 1000


@dataclass
class AdminTicket:
    ticket: str
    expires_at: int


class AdminTicketStore:
    def __init__(self) -> None:
        self._tickets: dict[str, AdminTicket] = {}

    def issue(self) -> AdminTicket:
        self._purge()
        item = AdminTicket(
            ticket=secrets.token_urlsafe(24),
            expires_at=utc_now_ms() + TICKET_TTL_MS,
        )
        self._tickets[item.ticket] = item
        return item

    def valid(self, ticket: str | None) -> bool:
        if not ticket:
            return False
        self._purge()
        item = self._tickets.get(ticket)
        if item is None:
            return False
        if item.expires_at <= utc_now_ms():
            self._tickets.pop(ticket, None)
            return False
        return True

    def _purge(self) -> None:
        now = utc_now_ms()
        dead = [k for k, v in self._tickets.items() if v.expires_at <= now]
        for key in dead:
            self._tickets.pop(key, None)


admin_ticket_store = AdminTicketStore()
