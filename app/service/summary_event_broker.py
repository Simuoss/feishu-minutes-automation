"""纪要生成的状态与流式内容分发。

按 (owner_user_id, minute_token) 隔离通道，避免多用户同妙记串流。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.core.resource_key import resource_key

logger = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE = 2000
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
    owner_user_id: int
    status: str = SummaryStatus.PENDING.value
    stage: str = "等待中"
    percent: int = 0
    attempt: int = 0
    max_attempts: int = 0
    queue_position: int = 0
    error_message: str | None = None
    buffer: str = ""
    plan_items: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = field(default_factory=_now_iso)
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)

    def snapshot(self) -> dict[str, Any]:
        return {
            "minute_token": self.minute_token,
            "owner_user_id": self.owner_user_id,
            "status": self.status,
            "stage": self.stage,
            "percent": self.percent,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "queue_position": self.queue_position,
            "error_message": self.error_message,
            "updated_at": self.updated_at,
        }


class BoundSummaryBroker:
    """绑定 owner+token 的薄包装，供生成流水线内大量调用。"""

    def __init__(
        self, broker: "SummaryEventBroker", minute_token: str, *, owner_user_id: int
    ) -> None:
        self._broker = broker
        self._token = minute_token
        self._owner = owner_user_id

    def queue(self, position: int = 0) -> None:
        self._broker.queue(self._token, owner_user_id=self._owner, position=position)

    def update(self, *, percent: int, stage: str) -> None:
        self._broker.update(
            self._token, owner_user_id=self._owner, percent=percent, stage=stage
        )

    def mark_attempt(
        self, attempt: int, max_attempts: int, *, label: str = "模型生成中"
    ) -> None:
        self._broker.mark_attempt(
            self._token,
            owner_user_id=self._owner,
            attempt=attempt,
            max_attempts=max_attempts,
            label=label,
        )

    def add_plan_item(self, timestamp: str, expect: str) -> None:
        self._broker.add_plan_item(
            self._token,
            owner_user_id=self._owner,
            timestamp=timestamp,
            expect=expect,
        )

    def clear_plan_items(self) -> None:
        self._broker.clear_plan_items(self._token, owner_user_id=self._owner)

    def append_delta(self, text: str) -> None:
        self._broker.append_delta(self._token, owner_user_id=self._owner, text=text)

    def reset_buffer(self) -> None:
        self._broker.reset_buffer(self._token, owner_user_id=self._owner)

    def complete(self, content: str, meta: dict[str, Any]) -> None:
        self._broker.complete(
            self._token, owner_user_id=self._owner, content=content, meta=meta
        )

    def fail(self, message: str) -> None:
        self._broker.fail(self._token, owner_user_id=self._owner, message=message)

    def get(self) -> SummaryChannel | None:
        return self._broker.get(self._token, owner_user_id=self._owner)


class SummaryEventBroker:
    """纪要生成状态的唯一来源，同时服务于轮询与 SSE。"""

    def __init__(self) -> None:
        self._channels: dict[str, SummaryChannel] = {}

    def bind(self, minute_token: str, *, owner_user_id: int) -> BoundSummaryBroker:
        return BoundSummaryBroker(self, minute_token, owner_user_id=owner_user_id)

    def _ensure(self, minute_token: str, *, owner_user_id: int) -> SummaryChannel:
        key = resource_key(owner_user_id, minute_token)
        channel = self._channels.get(key)
        if channel is None:
            channel = SummaryChannel(
                minute_token=minute_token, owner_user_id=owner_user_id
            )
            self._channels[key] = channel
        return channel

    def get(
        self, minute_token: str, *, owner_user_id: int
    ) -> SummaryChannel | None:
        return self._channels.get(resource_key(owner_user_id, minute_token))

    def buffer_of(self, minute_token: str, *, owner_user_id: int) -> str:
        channel = self.get(minute_token, owner_user_id=owner_user_id)
        return channel.buffer if channel else ""

    def clear(self, minute_token: str, *, owner_user_id: int) -> None:
        self._channels.pop(resource_key(owner_user_id, minute_token), None)

    def _broadcast(
        self, channel: SummaryChannel, event: str, data: dict[str, Any]
    ) -> None:
        if not channel.subscribers:
            return
        message = {"event": event, "data": data}
        for queue in list(channel.subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning(
                    "纪要事件订阅者队列已满 owner=%s token=%s，丢弃一条 %s 事件，"
                    "怀疑该客户端消费过慢或连接已僵死",
                    channel.owner_user_id,
                    channel.minute_token,
                    event,
                )

    def queue(
        self, minute_token: str, *, owner_user_id: int, position: int = 0
    ) -> None:
        channel = self._ensure(minute_token, owner_user_id=owner_user_id)
        if channel.status == SummaryStatus.GENERATING.value:
            return
        channel.status = SummaryStatus.QUEUED.value
        channel.percent = 0
        channel.attempt = 0
        channel.error_message = None
        channel.buffer = ""
        channel.queue_position = position
        channel.stage = (
            f"排队中，前面还有 {position} 个任务" if position > 0 else "排队中"
        )
        channel.updated_at = _now_iso()
        self._broadcast(channel, "status", channel.snapshot())

    def update_queue_position(
        self, minute_token: str, *, owner_user_id: int, position: int
    ) -> None:
        channel = self.get(minute_token, owner_user_id=owner_user_id)
        if channel is None or channel.status != SummaryStatus.QUEUED.value:
            return
        if channel.queue_position == position:
            return
        channel.queue_position = position
        channel.stage = (
            f"排队中，前面还有 {position} 个任务" if position > 0 else "排队中"
        )
        channel.updated_at = _now_iso()
        self._broadcast(channel, "status", channel.snapshot())

    def update(
        self, minute_token: str, *, owner_user_id: int, percent: int, stage: str
    ) -> None:
        channel = self._ensure(minute_token, owner_user_id=owner_user_id)
        channel.status = SummaryStatus.GENERATING.value
        channel.percent = max(0, min(100, percent))
        channel.stage = stage
        channel.queue_position = 0
        channel.updated_at = _now_iso()
        self._broadcast(channel, "status", channel.snapshot())

    def mark_attempt(
        self,
        minute_token: str,
        *,
        owner_user_id: int,
        attempt: int,
        max_attempts: int,
        label: str = "模型生成中",
    ) -> None:
        channel = self._ensure(minute_token, owner_user_id=owner_user_id)
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

    def add_plan_item(
        self,
        minute_token: str,
        *,
        owner_user_id: int,
        timestamp: str,
        expect: str,
    ) -> None:
        channel = self._ensure(minute_token, owner_user_id=owner_user_id)
        item = {
            "index": len(channel.plan_items) + 1,
            "timestamp": timestamp,
            "expect": expect,
        }
        channel.plan_items.append(item)
        channel.updated_at = _now_iso()
        self._broadcast(channel, "plan", item)

    def clear_plan_items(self, minute_token: str, *, owner_user_id: int) -> None:
        channel = self._ensure(minute_token, owner_user_id=owner_user_id)
        if not channel.plan_items:
            return
        channel.plan_items.clear()
        self._broadcast(channel, "plan_reset", {})

    def append_delta(
        self, minute_token: str, *, owner_user_id: int, text: str
    ) -> None:
        channel = self._ensure(minute_token, owner_user_id=owner_user_id)
        if channel.status == SummaryStatus.QUEUED.value:
            channel.status = SummaryStatus.GENERATING.value
            channel.queue_position = 0
            channel.stage = "模型生成中"
            channel.percent = max(channel.percent, 10)
        channel.buffer += text
        channel.updated_at = _now_iso()
        self._broadcast(channel, "delta", {"text": text})

    def reset_buffer(self, minute_token: str, *, owner_user_id: int) -> None:
        channel = self._ensure(minute_token, owner_user_id=owner_user_id)
        if not channel.buffer:
            return
        logger.info(
            "纪要生成重试，丢弃已产出的 %d 字并通知前端重来 owner=%s token=%s",
            len(channel.buffer),
            owner_user_id,
            minute_token,
        )
        channel.buffer = ""
        channel.updated_at = _now_iso()
        self._broadcast(channel, "reset", {})

    def complete(
        self,
        minute_token: str,
        *,
        owner_user_id: int,
        content: str,
        meta: dict[str, Any],
    ) -> None:
        channel = self._ensure(minute_token, owner_user_id=owner_user_id)
        channel.status = SummaryStatus.COMPLETED.value
        channel.percent = 100
        channel.stage = "生成完成"
        channel.error_message = None
        channel.buffer = ""
        channel.updated_at = _now_iso()
        self._broadcast(channel, "done", {"content": content, "meta": meta})

    def fail(
        self, minute_token: str, *, owner_user_id: int, message: str
    ) -> None:
        channel = self._ensure(minute_token, owner_user_id=owner_user_id)
        channel.status = SummaryStatus.FAILED.value
        channel.stage = "生成失败"
        channel.error_message = message
        channel.buffer = ""
        channel.updated_at = _now_iso()
        self._broadcast(channel, "failed", {"message": message})

    async def subscribe(
        self, minute_token: str, *, owner_user_id: int
    ) -> AsyncIterator[dict[str, Any]]:
        channel = self._ensure(minute_token, owner_user_id=owner_user_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=SUBSCRIBER_QUEUE_SIZE
        )
        channel.subscribers.add(queue)
        try:
            yield {
                "event": "snapshot",
                "data": {
                    **channel.snapshot(),
                    "buffer": channel.buffer,
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
