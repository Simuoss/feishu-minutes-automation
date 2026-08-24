from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.agent_chat import (
    AgentSessionCreateEntity,
    AgentSessionEntity,
)
from app.repository.orm.agent_session import AgentSessionORM


def _to_entity(orm: AgentSessionORM) -> AgentSessionEntity:
    return AgentSessionEntity(
        id=orm.id,
        sdk_session_id=orm.sdk_session_id,
        subject_type=orm.subject_type,
        subject_key=orm.subject_key,
        owner_user_id=orm.owner_user_id,
        scope=orm.scope,
        title=orm.title or "",
        turn_count=orm.turn_count or 0,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class AgentSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, entity: AgentSessionCreateEntity) -> AgentSessionEntity:
        orm = AgentSessionORM(
            sdk_session_id=entity.sdk_session_id,
            subject_type=entity.subject_type,
            subject_key=entity.subject_key,
            owner_user_id=entity.owner_user_id,
            scope=entity.scope,
            title=entity.title,
            turn_count=0,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def get(self, session_id: int) -> AgentSessionEntity | None:
        orm = await self._session.get(AgentSessionORM, session_id)
        return _to_entity(orm) if orm else None

    async def list_by_subject(
        self, subject_type: str, subject_key: str, *, limit: int = 50
    ) -> list[AgentSessionEntity]:
        stmt = (
            select(AgentSessionORM)
            .where(
                AgentSessionORM.subject_type == subject_type,
                AgentSessionORM.subject_key == subject_key,
            )
            .order_by(AgentSessionORM.updated_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def bump_turn(
        self, session_id: int, *, now_ms: int, title: str | None = None
    ) -> None:
        """记一轮提问。首轮顺手把标题落下来，列表页才有东西可显示。"""
        orm = await self._session.get(AgentSessionORM, session_id)
        if orm is None:
            return
        orm.turn_count = (orm.turn_count or 0) + 1
        orm.updated_at = now_ms
        if title and not (orm.title or "").strip():
            orm.title = title[:200]
        await self._session.flush()
