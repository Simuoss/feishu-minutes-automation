"""分享访客短期会话（进程内）。重启后需重新解锁。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from app.service.time_utils import utc_now_ms

SESSION_TTL_MS = 12 * 60 * 60 * 1000


@dataclass
class ShareSession:
    session_id: str
    share_id: int
    share_token: str
    minute_token: str
    access_key_id: int | None
    allow_export: bool
    expires_at: int


class ShareSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, ShareSession] = {}

    def create(
        self,
        *,
        share_id: int,
        share_token: str,
        minute_token: str,
        access_key_id: int | None,
        allow_export: bool,
    ) -> ShareSession:
        self._purge_expired()
        session = ShareSession(
            session_id=secrets.token_urlsafe(24),
            share_id=share_id,
            share_token=share_token,
            minute_token=minute_token,
            access_key_id=access_key_id,
            allow_export=allow_export,
            expires_at=utc_now_ms() + SESSION_TTL_MS,
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None) -> ShareSession | None:
        if not session_id:
            return None
        self._purge_expired()
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.expires_at <= utc_now_ms():
            self._sessions.pop(session_id, None)
            return None
        return session

    def invalidate_share(self, share_id: int) -> int:
        """权限变更或吊销后清掉该分享的访客会话。"""
        dead = [sid for sid, s in self._sessions.items() if s.share_id == share_id]
        for sid in dead:
            self._sessions.pop(sid, None)
        return len(dead)

    def invalidate_access_key(self, access_key_id: int) -> int:
        """吊销密钥后清掉所有依赖该密钥的访客会话。"""
        dead = [
            sid
            for sid, s in self._sessions.items()
            if s.access_key_id == access_key_id
        ]
        for sid in dead:
            self._sessions.pop(sid, None)
        return len(dead)

    def _purge_expired(self) -> None:
        now = utc_now_ms()
        dead = [sid for sid, s in self._sessions.items() if s.expires_at <= now]
        for sid in dead:
            self._sessions.pop(sid, None)


share_session_store = ShareSessionStore()
