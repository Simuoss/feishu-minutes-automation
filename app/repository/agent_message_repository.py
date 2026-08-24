from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.agent_chat import (
    AgentMessageCreateEntity,
    AgentMessageEntity,
)
from app.repository.json_map import dump_json, load_json_dict
from app.repository.orm.agent_message import AgentMessageORM


def _to_entity(orm: AgentMessageORM) -> AgentMessageEntity:
    return AgentMessageEntity(
        id=orm.id,
        session_id=orm.session_id,
        seq=orm.seq,
        role=orm.role,
        content=orm.content or "",
        meta=load_json_dict(orm.meta_json),
        created_at=orm.created_at,
    )


class AgentMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, entity: AgentMessageCreateEntity) -> AgentMessageEntity:
        stmt = select(func.max(AgentMessageORM.seq)).where(
            AgentMessageORM.session_id == entity.session_id
        )
        result = await self._session.execute(stmt)
        next_seq = int(result.scalar() or 0) + 1
        orm = AgentMessageORM(
            session_id=entity.session_id,
            seq=next_seq,
            role=entity.role,
            content=entity.content,
            meta_json=dump_json(entity.meta or None),
            created_at=entity.created_at,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def list_by_session(
        self, session_id: int, *, limit: int = 500
    ) -> list[AgentMessageEntity]:
        stmt = (
            select(AgentMessageORM)
            .where(AgentMessageORM.session_id == session_id)
            .order_by(AgentMessageORM.seq.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]
