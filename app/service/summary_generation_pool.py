"""纪要生成作业协调：按 (owner, token) 单飞。

真正限制「同时有多少次大模型 HTTP 调用」的是 llm_call_pool（读 LLM_CONCURRENCY）。
本模块不再做作业级并发信号量，只保证同一会议不会并行跑两条纪要流水线；
重复 submit 会 await 已有 in-flight future。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from app.core.resource_key import resource_key
from app.service.summary_event_broker import summary_broker

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SummaryGenerationPool:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gate: asyncio.Lock | None = None
        self._running: set[str] = set()
        self._inflight: dict[str, asyncio.Future[Any]] = {}

    def reconfigure_from_runtime(self) -> None:
        """保留接口：作业侧不再读并发配置，限流由 llm_call_pool 负责。"""
        return

    def _ensure_loop_state(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._gate = asyncio.Lock()
            self._loop = loop
            self._running.clear()
            self._inflight.clear()
        assert self._gate is not None
        return loop

    def _get_gate(self) -> asyncio.Lock:
        self._ensure_loop_state()
        assert self._gate is not None
        return self._gate

    def is_active(self, minute_token: str, *, owner_user_id: int) -> bool:
        key = resource_key(owner_user_id, minute_token)
        return key in self._inflight or key in self._running

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def waiting_count(self) -> int:
        # 作业侧不再排队；等待发生在全局 LLM 槽上
        return 0

    def sync_broker_status(
        self, minute_token: str, *, owner_user_id: int
    ) -> None:
        key = resource_key(owner_user_id, minute_token)
        if key not in self._running and key not in self._inflight:
            return
        channel = summary_broker.get(minute_token, owner_user_id=owner_user_id)
        stage = (
            channel.stage
            if channel and channel.stage not in {"排队中", "等待中"}
            else "模型生成中"
        )
        percent = channel.percent if channel else 10
        summary_broker.update(
            minute_token,
            owner_user_id=owner_user_id,
            percent=max(percent, 10),
            stage=stage,
        )

    async def submit(
        self,
        minute_token: str,
        task: Callable[[], Awaitable[T]],
        *,
        owner_user_id: int,
    ) -> T:
        key = resource_key(owner_user_id, minute_token)
        loop = self._ensure_loop_state()

        async with self._get_gate():
            existing = self._inflight.get(key)
            if existing is not None:
                logger.info(
                    "纪要生成任务已在进行，附身已有 future owner=%s token=%s",
                    owner_user_id,
                    minute_token,
                )
                fut: asyncio.Future[Any] = existing
                is_leader = False
            else:
                fut = loop.create_future()
                self._inflight[key] = fut
                is_leader = True

        if not is_leader:
            return await asyncio.shield(fut)

        self._running.add(key)
        try:
            try:
                result = await task()
            except Exception as exc:
                logger.exception(
                    "纪要生成任务异常退出 owner=%s token=%s，将标记失败以便重新提交。原因：%s",
                    owner_user_id,
                    minute_token,
                    exc,
                )
                summary_broker.fail(
                    minute_token,
                    owner_user_id=owner_user_id,
                    message=f"生成中断：{exc}",
                )
                raise
            if not fut.done():
                fut.set_result(result)
            return result
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            self._running.discard(key)
            async with self._get_gate():
                if self._inflight.get(key) is fut:
                    self._inflight.pop(key, None)


summary_generation_pool = SummaryGenerationPool()
