"""把后台线程里的协程安全投递到 FastAPI 主事件循环。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def get_main_loop() -> asyncio.AbstractEventLoop | None:
    return _main_loop


async def _run_logged(coro: Coroutine[Any, Any, Any], *, what: str) -> None:
    try:
        await coro
    except Exception:
        logger.exception("%s 主循环任务失败", what)


def schedule_on_main_loop(coro: Coroutine[Any, Any, Any], *, what: str) -> None:
    """从任意线程投递到主 loop；主 loop 未就绪时退回当前 running loop。"""
    loop = _main_loop
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(_run_logged(coro, what=what), loop)
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(coro)
        except Exception:
            logger.exception("%s 无事件循环，同步执行仍失败", what)
        return
    running.create_task(_run_logged(coro, what=what))
