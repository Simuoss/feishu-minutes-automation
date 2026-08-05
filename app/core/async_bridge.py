"""在同步调用点调度/执行协程（双写与 Token 落库用）。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # 已在事件循环内：开线程跑独立 loop，避免嵌套 await
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def schedule_async(coro: Coroutine[Any, Any, Any], *, what: str) -> None:
    """尽量不阻塞主流程；失败只打日志。"""

    def _done(task: asyncio.Task[Any]) -> None:
        exc = task.exception()
        if exc is not None:
            logger.error(
                "%s 异步双写失败，主流程已继续；怀疑 DB 锁或 schema 未迁移。err=%s",
                what,
                exc,
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "%s 同步双写失败，主流程已继续；怀疑 DB 不可用。err=%s",
                what,
                exc,
            )
        return

    task = loop.create_task(coro)
    task.add_done_callback(_done)
