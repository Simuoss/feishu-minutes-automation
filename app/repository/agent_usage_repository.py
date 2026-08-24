from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.agent_chat import AgentUsageEntity
from app.repository.orm.agent_daily_usage import AgentDailyUsageORM


class AgentUsageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, subject_type: str, subject_key: str, day: str
    ) -> AgentUsageEntity:
        stmt = select(AgentDailyUsageORM).where(
            AgentDailyUsageORM.subject_type == subject_type,
            AgentDailyUsageORM.subject_key == subject_key,
            AgentDailyUsageORM.day == day,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return AgentUsageEntity(
            subject_type=subject_type,
            subject_key=subject_key,
            day=day,
            used=int(orm.used or 0) if orm else 0,
        )

    async def increment(
        self, subject_type: str, subject_key: str, day: str, *, now_ms: int
    ) -> int:
        """加一并返回加完之后的用量。"""
        stmt = select(AgentDailyUsageORM).where(
            AgentDailyUsageORM.subject_type == subject_type,
            AgentDailyUsageORM.subject_key == subject_key,
            AgentDailyUsageORM.day == day,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            orm = AgentDailyUsageORM(
                subject_type=subject_type,
                subject_key=subject_key,
                day=day,
                used=0,
                updated_at=now_ms,
            )
            self._session.add(orm)
        orm.used = int(orm.used or 0) + 1
        orm.updated_at = now_ms
        await self._session.flush()
        return int(orm.used)
