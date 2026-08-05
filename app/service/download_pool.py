"""妙记下载的并发池。

批量下载与事件触发下载共用同一上限，避免同时拉太多媒体把带宽和飞书接口打满。
超出并发的任务排队，并把排队位置写进下载进度，供列表页展示。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.core.config import settings
from app.service.download_progress_store import download_progress_store

logger = logging.getLogger(__name__)

T = TypeVar("T")


class DownloadPool:
    def __init__(self, concurrency: int | None = None) -> None:
        self._concurrency = (
            settings.download_concurrency if concurrency is None else concurrency
        )
        self._semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiting: list[str] = []
        self._running: set[str] = set()

    def _get_semaphore(self) -> asyncio.Semaphore:
        # Semaphore 绑定首次使用它的事件循环；测试里每个用例各起一个，按循环重建。
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
            stage = "排队中" if index == 0 else f"排队中（前面还有 {index} 个）"
            download_progress_store.update(token, percent=0, stage=stage)

    async def submit(self, minute_token: str, task: Callable[[], Awaitable[T]]) -> T:
        """在池中执行一个下载任务，超出并发时排队等待。"""
        self._waiting.append(minute_token)
        position = len(self._waiting) - 1
        self._publish_positions()
        if position > 0:
            logger.info(
                "下载任务排队 token=%s，前面还有 %d 个，当前并发上限 %d",
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
                finally:
                    self._running.discard(minute_token)
                    self._publish_positions()
        finally:
            if minute_token in self._waiting:
                self._waiting.remove(minute_token)
                self._publish_positions()


download_pool = DownloadPool()
