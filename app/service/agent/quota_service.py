"""检索助手的每日配额。

阈值在 system_configs 里按主体类型配，0 表示不限。不限的主体也照样计数，
方便事后回看谁用了多少。日界按 Asia/Shanghai 切，贴合用户对「今天」的直觉。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core import runtime_config
from app.core.config import settings
from app.data_model.entity.agent_chat import (
    SUBJECT_ACCESS_KEY,
    SUBJECT_ANON,
    SUBJECT_USER,
)
from app.repository.uow import UnitOfWork
from app.service.time_utils import utc_now_ms

logger = logging.getLogger(__name__)

# 面向用户的「每日」按北京时间算，不用 UTC
_LOCAL_TZ = timezone(timedelta(hours=8))

_QUOTA_KEYS = {
    SUBJECT_USER: ("AGENT_DAILY_QUOTA_USER", "agent_daily_quota_user"),
    SUBJECT_ACCESS_KEY: ("AGENT_DAILY_QUOTA_KEY", "agent_daily_quota_key"),
    SUBJECT_ANON: ("AGENT_DAILY_QUOTA_ANON", "agent_daily_quota_anon"),
}


@dataclass(frozen=True)
class Subject:
    """提问主体。subject_key 必须是稳定的，否则配额一换就失效。"""

    type: str
    key: str

    @staticmethod
    def for_user(user_id: int) -> "Subject":
        return Subject(type=SUBJECT_USER, key=str(user_id))

    @staticmethod
    def for_access_keys(key_hashes: list[str]) -> "Subject":
        """一个访客手上的全部可用密钥算一个主体，增减密钥才会换主体。"""
        joined = "|".join(sorted(set(key_hashes)))
        digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]
        return Subject(type=SUBJECT_ACCESS_KEY, key=digest)

    @staticmethod
    def for_anonymous(share_session: str | None, ip: str) -> "Subject":
        raw = f"{(share_session or '').strip()}|{ip.strip()}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return Subject(type=SUBJECT_ANON, key=digest)


@dataclass
class QuotaState:
    limit: int
    used: int

    @property
    def unlimited(self) -> bool:
        return self.limit <= 0

    @property
    def exceeded(self) -> bool:
        return not self.unlimited and self.used >= self.limit

    @property
    def remaining(self) -> int | None:
        if self.unlimited:
            return None
        return max(0, self.limit - self.used)


class AgentQuotaExceeded(Exception):
    def __init__(self, state: QuotaState) -> None:
        self.state = state
        super().__init__(
            f"今天的提问次数已经用完了（上限 {state.limit} 次），明天再来。"
        )


def today() -> str:
    return datetime.now(_LOCAL_TZ).strftime("%Y-%m-%d")


def limit_for(subject_type: str) -> int:
    config_key, settings_attr = _QUOTA_KEYS.get(
        subject_type, _QUOTA_KEYS[SUBJECT_ANON]
    )
    fallback = int(getattr(settings, settings_attr, 0) or 0)
    return max(0, runtime_config.get_int(config_key, fallback))


async def peek(subject: Subject) -> QuotaState:
    day = today()
    async with UnitOfWork() as uow:
        assert uow.agent_usage is not None
        usage = await uow.agent_usage.get(subject.type, subject.key, day)
    return QuotaState(limit=limit_for(subject.type), used=usage.used)


async def consume(subject: Subject) -> QuotaState:
    """先看额度再计数；超了直接抛，别把请求放进去白烧钱。"""
    day = today()
    limit = limit_for(subject.type)
    async with UnitOfWork() as uow:
        assert uow.agent_usage is not None
        current = await uow.agent_usage.get(subject.type, subject.key, day)
        state = QuotaState(limit=limit, used=current.used)
        if state.exceeded:
            logger.info(
                "检索助手配额用尽 subject=%s/%s used=%s limit=%s",
                subject.type,
                subject.key,
                current.used,
                limit,
            )
            raise AgentQuotaExceeded(state)
        used = await uow.agent_usage.increment(
            subject.type, subject.key, day, now_ms=utc_now_ms()
        )
        await uow.commit()
    return QuotaState(limit=limit, used=used)
