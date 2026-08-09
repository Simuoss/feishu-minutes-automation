"""飞书 SSO 回调后的一次性短码：前端兑换本系统 JWT，避免 JWT 出现在 URL 日志。"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

_TTL_SECONDS = 120


@dataclass
class SsoTicketPayload:
    user_id: int
    username: str
    setup_name: bool
    expires_at: float


class SsoTicketStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, SsoTicketPayload] = {}

    def issue(
        self, *, user_id: int, username: str, setup_name: bool
    ) -> str:
        self._purge()
        ticket = secrets.token_urlsafe(24)
        with self._lock:
            self._items[ticket] = SsoTicketPayload(
                user_id=user_id,
                username=username,
                setup_name=setup_name,
                expires_at=time.time() + _TTL_SECONDS,
            )
        return ticket

    def consume(self, ticket: str) -> SsoTicketPayload | None:
        key = (ticket or "").strip()
        if not key:
            return None
        with self._lock:
            payload = self._items.pop(key, None)
        if payload is None:
            return None
        if payload.expires_at < time.time():
            return None
        return payload

    def _purge(self) -> None:
        now = time.time()
        with self._lock:
            dead = [k for k, v in self._items.items() if v.expires_at < now]
            for k in dead:
                self._items.pop(k, None)


sso_ticket_store = SsoTicketStore()
