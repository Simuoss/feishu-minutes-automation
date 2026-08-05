"""纪要生成的并发池。

模型服务对并发很敏感（打满会直接 429），所以同时在跑的生成任务数量必须受控，
其余任务排队并把队列位置反馈给前端。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.core.config import settings
from app.service.summary_event_broker import summary_broker

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SummaryGenerationPool:
    def __init__(self, concurrency: int | None = None) -> None:
        self._concurrency = concurrency or settings.llm_concurrency
        self._semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiting: list[str] = []
        self._running: set[str] = set()

    def _get_semaphore(self) -> asyncio.Semaphore:
        # Semaphore 会绑定首次使用它的事件循环；生产中只有一个循环，
        # 但测试里每个用例各起一个，这里按循环重建以避免跨循环复用报错。
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._loop is not loop:
            self._semaphore = asyncio.Semaphore(self._concurrency)
            self._loop = loop
        return self._semaphore

    def is_active(self, minute_token: str) -> bool:
        return minute_token in self._waiting or minute_token in self._running

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    def _publish_positions(self) -> None:
        for index, token in enumerate(self._waiting):
            summary_broker.update_queue_position(token, index)

    def sync_broker_status(self, minute_token: str) -> None:
        """把池内真实状态同步到 broker，避免重复提交后 UI 仍显示排队中。"""
        if minute_token in self._running:
            channel = summary_broker.get(minute_token)
            stage = channel.stage if channel and channel.stage not in {"排队中", "等待中"} else "模型生成中"
            percent = channel.percent if channel else 10
            summary_broker.update(minute_token, percent=max(percent, 10), stage=stage)
            return
        if minute_token in self._waiting:
            position = self._waiting.index(minute_token)
            summary_broker.queue(minute_token, position)

    async def submit(self, minute_token: str, task: Callable[[], Awaitable[T]]) -> T:
        """在池中执行一个生成任务，超出并发时排队等待。"""
        self._waiting.append(minute_token)
        position = len(self._waiting) - 1
        summary_broker.queue(minute_token, position)
        if position > 0:
            logger.info(
                "纪要生成任务排队 token=%s，前面还有 %d 个，当前并发上限 %d",
                minute_token,
                position,
                self._concurrency,
            )

        try:
            async with self._get_semaphore():
                if minute_token in self._waiting:
                    self._waiting.remove(minute_token)
                self._running.add(minute_token)
                self._publish_positions()
                try:
                    return await task()
                except Exception as exc:
                    # 未捕获异常会让 broker 停在最后一个 stage，前端表现为“永久生成中”
                    logger.exception(
                        "纪要生成任务异常退出 token=%s，将标记失败以便重新提交。原因：%s",
                        minute_token,
                        exc,
                    )
                    summary_broker.fail(minute_token, f"生成中断：{exc}")
                    raise
                finally:
                    self._running.discard(minute_token)
                    self._publish_positions()
        finally:
            if minute_token in self._waiting:
                self._waiting.remove(minute_token)
                self._publish_positions()


summary_generation_pool = SummaryGenerationPool()
