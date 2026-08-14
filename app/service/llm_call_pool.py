"""全局大模型调用并发池。

所有走 LlmClient.complete / complete_messages 的 HTTP 请求共用本池，
包括纪要成文、挑图、脱敏、划词答疑等，避免各业务各自打满上游配额。

业务侧若有多段可并行的模型调用，应 asyncio.gather 一并发起，
由本池的 Semaphore 做全局限流；不要在业务层再串行 await 人为降并发。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token

from app.core import runtime_config

logger = logging.getLogger(__name__)

_wait_stage_hook: ContextVar[Callable[[str], None] | None] = ContextVar(
    "llm_wait_stage_hook", default=None
)


def set_wait_stage_hook(hook: Callable[[str], None]) -> Token[Callable[[str], None] | None]:
    return _wait_stage_hook.set(hook)


def reset_wait_stage_hook(token: Token[Callable[[str], None] | None]) -> None:
    _wait_stage_hook.reset(token)


class LlmCallPool:
    def __init__(self, concurrency: int | None = None) -> None:
        self._fixed_concurrency = concurrency
        self._concurrency = concurrency or runtime_config.get_int("LLM_CONCURRENCY", 20)
        self._semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._in_flight = 0
        self._waiters = 0

    def reconfigure_from_runtime(self) -> None:
        if self._fixed_concurrency is not None:
            return
        next_n = max(1, runtime_config.get_int("LLM_CONCURRENCY", 20))
        if next_n == self._concurrency and self._semaphore is not None:
            return
        self._concurrency = next_n
        # 仅在空闲时重建，避免打断进行中的调用
        if self._loop is not None and self._in_flight == 0 and self._waiters == 0:
            self._semaphore = asyncio.Semaphore(self._concurrency)
            logger.info("全局大模型调用并发池已调整为 %s", self._concurrency)

    def _ensure_loop_state(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            if self._fixed_concurrency is None:
                self._concurrency = max(
                    1, runtime_config.get_int("LLM_CONCURRENCY", 20)
                )
            self._semaphore = asyncio.Semaphore(self._concurrency)
            self._loop = loop
            self._in_flight = 0
            self._waiters = 0
            logger.info(
                "全局大模型调用并发池已初始化，上限 %s", self._concurrency
            )

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def remaining(self) -> int:
        return max(0, self._concurrency - self._in_flight)

    @property
    def waiters(self) -> int:
        return self._waiters

    def stats(self) -> dict[str, int]:
        """供进度接口展示：总量 / 占用 / 剩余 / 正在等槽的协程数。"""
        total = max(1, self._concurrency)
        busy = max(0, self._in_flight)
        return {
            "llm_slots_total": total,
            "llm_slots_busy": busy,
            "llm_slots_free": max(0, total - busy),
            "llm_waiters": max(0, self._waiters),
        }

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """占用一个大模型调用名额；等待期间计入 waiters，不计入 in_flight。"""
        self._ensure_loop_state()
        assert self._semaphore is not None
        self._waiters += 1
        hook = _wait_stage_hook.get()
        if hook is not None and self._in_flight >= self._concurrency:
            hook("在等模型槽位")
        try:
            await self._semaphore.acquire()
        finally:
            self._waiters = max(0, self._waiters - 1)
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight = max(0, self._in_flight - 1)
            self._semaphore.release()


llm_call_pool = LlmCallPool()
