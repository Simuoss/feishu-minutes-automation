"""纪要生成的并发池。按 (owner, token) 单飞与排队。

同一 key 只跑一个任务：is_active 与 submit 之间通过 inflight dict + lock 原子化；
重复 submit 会 await 已有 in-flight future。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from app.core.config import settings
from app.core.resource_key import resource_key
from app.service.summary_event_broker import summary_broker

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SummaryGenerationPool:
    def __init__(self, concurrency: int | None = None) -> None:
        self._concurrency = concurrency or settings.llm_concurrency
        self._semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gate: asyncio.Lock | None = None
        self._waiting: list[str] = []
        self._running: set[str] = set()
        self._key_meta: dict[str, tuple[int, str]] = {}
        self._inflight: dict[str, asyncio.Future[Any]] = {}

    def _ensure_loop_state(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._semaphore = asyncio.Semaphore(self._concurrency)
            self._gate = asyncio.Lock()
            self._loop = loop
            self._waiting.clear()
            self._running.clear()
            self._key_meta.clear()
            self._inflight.clear()
        assert self._semaphore is not None
        assert self._gate is not None
        return loop

    def _get_semaphore(self) -> asyncio.Semaphore:
        self._ensure_loop_state()
        assert self._semaphore is not None
        return self._semaphore

    def _get_gate(self) -> asyncio.Lock:
        self._ensure_loop_state()
        assert self._gate is not None
        return self._gate

    def is_active(self, minute_token: str, *, owner_user_id: int) -> bool:
        key = resource_key(owner_user_id, minute_token)
        return key in self._inflight or key in self._waiting or key in self._running

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    def _publish_positions(self) -> None:
        for index, key in enumerate(self._waiting):
            meta = self._key_meta.get(key)
            if meta is None:
                continue
            owner_user_id, token = meta
            summary_broker.update_queue_position(
                token, owner_user_id=owner_user_id, position=index
            )

    def sync_broker_status(
        self, minute_token: str, *, owner_user_id: int
    ) -> None:
        key = resource_key(owner_user_id, minute_token)
        if key in self._running or key in self._inflight:
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
            return
        if key in self._waiting:
            position = self._waiting.index(key)
            summary_broker.queue(
                minute_token, owner_user_id=owner_user_id, position=position
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

        self._key_meta[key] = (owner_user_id, minute_token)
        self._waiting.append(key)
        position = len(self._waiting) - 1
        summary_broker.queue(
            minute_token, owner_user_id=owner_user_id, position=position
        )
        if position > 0:
            logger.info(
                "纪要生成任务排队 owner=%s token=%s，前面还有 %d 个，当前并发上限 %d",
                owner_user_id,
                minute_token,
                position,
                self._concurrency,
            )

        try:
            async with self._get_semaphore():
                if key in self._waiting:
                    self._waiting.remove(key)
                self._running.add(key)
                self._publish_positions()
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
                finally:
                    self._running.discard(key)
                    self._publish_positions()
            if not fut.done():
                fut.set_result(result)
            return result
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            if key in self._waiting:
                self._waiting.remove(key)
                self._publish_positions()
            async with self._get_gate():
                if self._inflight.get(key) is fut:
                    self._inflight.pop(key, None)


summary_generation_pool = SummaryGenerationPool()
