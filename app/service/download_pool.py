"""妙记下载的并发池。

批量下载与事件触发下载共用同一上限，避免同时拉太多媒体把带宽和飞书接口打满。
超出并发的任务排队，并把排队位置写进下载进度，供列表页展示。
同一 (owner, token) 单飞：重复 submit 会 await 已有 in-flight future。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from app.core.config import settings
from app.core.resource_key import resource_key
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
        self._gate: asyncio.Lock | None = None
        # 元素为 resource_key(owner, token)
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
            stage = "排队中" if index == 0 else f"排队中（前面还有 {index} 个）"
            download_progress_store.update(
                token, owner_user_id=owner_user_id, percent=0, stage=stage
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
                    "下载任务已在进行，附身已有 future owner=%s token=%s",
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
        self._publish_positions()
        if position > 0:
            logger.info(
                "下载任务排队 owner=%s token=%s，前面还有 %d 个，当前并发上限 %d",
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


download_pool = DownloadPool()
