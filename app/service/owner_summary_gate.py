"""同一本地用户同时只跑一场纪要，避免互相打满模型配额。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager


class OwnerSummaryGate:
    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _lock_for(self, owner_user_id: int) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._locks.clear()
            self._loop = loop
        lock = self._locks.get(owner_user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[owner_user_id] = lock
        return lock

    def is_busy(self, owner_user_id: int) -> bool:
        lock = self._locks.get(owner_user_id)
        return bool(lock and lock.locked())

    @asynccontextmanager
    async def hold(
        self,
        owner_user_id: int,
        *,
        on_wait: Callable[[str], None] | None = None,
    ) -> AsyncIterator[None]:
        lock = self._lock_for(owner_user_id)
        if lock.locked() and on_wait is not None:
            on_wait("同账号上一场纪要进行中，排队等待")
        async with lock:
            yield


owner_summary_gate = OwnerSummaryGate()
