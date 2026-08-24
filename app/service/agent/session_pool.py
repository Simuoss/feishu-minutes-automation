"""检索助手的并发闸。

每个会话背后是一个 CLI 子进程，比普通 HTTP 调用重得多，所以单独一个池，
不跟纪要那边的 LLM_CONCURRENCY 抢。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core import runtime_config
from app.core.config import settings

logger = logging.getLogger(__name__)


class AgentSessionPool:
    def __init__(self) -> None:
        self._semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._concurrency = 0
        self._in_flight = 0

    def _ensure(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._concurrency = max(
                1, runtime_config.get_int("AGENT_CONCURRENCY", settings.agent_concurrency)
            )
            self._semaphore = asyncio.Semaphore(self._concurrency)
            self._loop = loop
            self._in_flight = 0
            logger.info("检索助手并发池已初始化，上限 %s", self._concurrency)

    @property
    def busy(self) -> int:
        return self._in_flight

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[bool]:
        """占一个名额；yield 出来的布尔表示是否等过队，用来给前端提示。"""
        self._ensure()
        assert self._semaphore is not None
        waited = self._in_flight >= self._concurrency
        await self._semaphore.acquire()
        self._in_flight += 1
        try:
            yield waited
        finally:
            self._in_flight = max(0, self._in_flight - 1)
            self._semaphore.release()


agent_session_pool = AgentSessionPool()
