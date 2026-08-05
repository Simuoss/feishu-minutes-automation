"""纪要生成的状态与流式内容分发。

生成一次耗时数分钟，用户可能中途打开页面，也可能开着页面等。因此这里为每个会议
维护一条通道：既保存当前状态与已生成文本的快照（供新订阅者立即追上进度），
也向所有在线订阅者广播增量。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# 单个订阅者积压过多说明前端消费不过来，丢弃增量即可，最终 done 事件会带上全文
SUBSCRIBER_QUEUE_SIZE = 2000

# 生成期间可能长时间没有任何事件，定期发心跳以免连接被中间层判定为空闲而关闭
HEARTBEAT_SECONDS = 15.0


class SummaryStatus(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SummaryChannel:
    minute_token: str
    status: str = SummaryStatus.PENDING.value
    stage: str = "等待中"
    percent: int = 0
    attempt: int = 0
    max_attempts: int = 0
    queue_position: int = 0
    error_message: str | None = None
    buffer: str = ""
    # 模型边读转写边挑出的截图时间点，供前端在正文开始前展示进展
    plan_items: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = field(default_factory=_now_iso)
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)

    def snapshot(self) -> dict[str, Any]:
        return {
            "minute_token": self.minute_token,
            "status": self.status,
            "stage": self.stage,
            "percent": self.percent,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "queue_position": self.queue_position,
            "error_message": self.error_message,
            "updated_at": self.updated_at,
        }


class SummaryEventBroker:
    """纪要生成状态的唯一来源，同时服务于轮询与 SSE。"""

    def __init__(self) -> None:
        self._channels: dict[str, SummaryChannel] = {}

    def _ensure(self, minute_token: str) -> SummaryChannel:
        channel = self._channels.get(minute_token)
        if channel is None:
            channel = SummaryChannel(minute_token=minute_token)
            self._channels[minute_token] = channel
        return channel

    def get(self, minute_token: str) -> SummaryChannel | None:
        return self._channels.get(minute_token)

    def buffer_of(self, minute_token: str) -> str:
        channel = self._channels.get(minute_token)
        return channel.buffer if channel else ""

    def clear(self, minute_token: str) -> None:
        self._channels.pop(minute_token, None)

    def _broadcast(self, channel: SummaryChannel, event: str, data: dict[str, Any]) -> None:
        if not channel.subscribers:
            return
        message = {"event": event, "data": data}
        for queue in list(channel.subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning(
                    "纪要事件订阅者队列已满 token=%s，丢弃一条 %s 事件，"
                    "怀疑该客户端消费过慢或连接已僵死",
                    channel.minute_token,
                    event,
                )

    def queue(self, minute_token: str, position: int = 0) -> None:
        channel = self._ensure(minute_token)
        # 已在生成中的任务不要被 queue 打回排队态并清掉已流式输出的缓冲
        if channel.status == SummaryStatus.GENERATING.value:
            return
        channel.status = SummaryStatus.QUEUED.value
        channel.percent = 0
        channel.attempt = 0
        channel.error_message = None
        channel.buffer = ""
        channel.queue_position = position
        channel.stage = f"排队中，前面还有 {position} 个任务" if position > 0 else "排队中"
        channel.updated_at = _now_iso()
        self._broadcast(channel, "status", channel.snapshot())

    def update_queue_position(self, minute_token: str, position: int) -> None:
        channel = self._channels.get(minute_token)
        if channel is None or channel.status != SummaryStatus.QUEUED.value:
            return
        if channel.queue_position == position:
            return
        channel.queue_position = position
        channel.stage = f"排队中，前面还有 {position} 个任务" if position > 0 else "排队中"
        channel.updated_at = _now_iso()
        self._broadcast(channel, "status", channel.snapshot())

    def update(self, minute_token: str, *, percent: int, stage: str) -> None:
        channel = self._ensure(minute_token)
        channel.status = SummaryStatus.GENERATING.value
        channel.percent = max(0, min(100, percent))
        channel.stage = stage
        channel.queue_position = 0
        channel.updated_at = _now_iso()
        self._broadcast(channel, "status", channel.snapshot())

    def mark_attempt(
        self,
        minute_token: str,
        attempt: int,
        max_attempts: int,
        *,
        label: str = "模型生成中",
    ) -> None:
        channel = self._ensure(minute_token)
        channel.status = SummaryStatus.GENERATING.value
        channel.attempt = attempt
        channel.max_attempts = max_attempts
        channel.percent = max(channel.percent, 10)
        channel.queue_position = 0
        channel.stage = (
            label
            if attempt == 1
            else f"模型服务不稳定，正在第 {attempt}/{max_attempts} 次重试"
        )
        channel.updated_at = _now_iso()
        self._broadcast(channel, "status", channel.snapshot())

    def add_plan_item(self, minute_token: str, timestamp: str, expect: str) -> None:
        """模型每挑中一处画面就推一条，让规划阶段不再是干等的黑箱。"""
        channel = self._ensure(minute_token)
        item = {"index": len(channel.plan_items) + 1, "timestamp": timestamp, "expect": expect}
        channel.plan_items.append(item)
        channel.updated_at = _now_iso()
        self._broadcast(channel, "plan", item)

    def clear_plan_items(self, minute_token: str) -> None:
        channel = self._ensure(minute_token)
        if not channel.plan_items:
            return
        channel.plan_items.clear()
        self._broadcast(channel, "plan_reset", {})

    def append_delta(self, minute_token: str, text: str) -> None:
        channel = self._ensure(minute_token)
        # 收到正文增量说明模型已在输出，不应再停留在排队态
        if channel.status == SummaryStatus.QUEUED.value:
            channel.status = SummaryStatus.GENERATING.value
            channel.queue_position = 0
            channel.stage = "模型生成中"
            channel.percent = max(channel.percent, 10)
        channel.buffer += text
        channel.updated_at = _now_iso()
        self._broadcast(channel, "delta", {"text": text})

    def reset_buffer(self, minute_token: str) -> None:
        channel = self._ensure(minute_token)
        if not channel.buffer:
            return
        logger.info(
            "纪要生成重试，丢弃已产出的 %d 字并通知前端重来 token=%s",
            len(channel.buffer),
            minute_token,
        )
        channel.buffer = ""
        channel.updated_at = _now_iso()
        self._broadcast(channel, "reset", {})

    def complete(self, minute_token: str, content: str, meta: dict[str, Any]) -> None:
        channel = self._ensure(minute_token)
        channel.status = SummaryStatus.COMPLETED.value
        channel.percent = 100
        channel.stage = "生成完成"
        channel.error_message = None
        # 正文已落盘，通道里不再保留副本
        channel.buffer = ""
        channel.updated_at = _now_iso()
        self._broadcast(channel, "done", {"content": content, "meta": meta})

    def fail(self, minute_token: str, message: str) -> None:
        channel = self._ensure(minute_token)
        channel.status = SummaryStatus.FAILED.value
        channel.stage = "生成失败"
        channel.error_message = message
        channel.buffer = ""
        channel.updated_at = _now_iso()
        # 事件名不用 error：EventSource 的内置连接错误事件同名，前端无法区分
        self._broadcast(channel, "failed", {"message": message})

    async def subscribe(self, minute_token: str) -> AsyncIterator[dict[str, Any]]:
        """订阅某个会议的生成事件，首帧为当前状态与已生成内容的快照。"""
        channel = self._ensure(minute_token)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        channel.subscribers.add(queue)
        try:
            yield {
                "event": "snapshot",
                "data": {
                    **channel.snapshot(),
                    "buffer": channel.buffer,
                    # 中途接入的客户端也要看到已经挑好的时间点
                    "plan_items": list(channel.plan_items),
                },
            }
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield {"event": "ping", "data": {}}
        finally:
            channel.subscribers.discard(queue)


summary_broker = SummaryEventBroker()
