from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.recording_session import (
    RecordingSessionCreateEntity,
    RecordingSessionEntity,
    RecordingSessionUpdateEntity,
)
from app.repository.orm.recording_session import RecordingSessionORM


def _to_entity(orm: RecordingSessionORM) -> RecordingSessionEntity:
    return RecordingSessionEntity(
        id=orm.id,
        unique_key=orm.unique_key,
        minute_token=orm.minute_token,
        meeting_id=orm.meeting_id,
        object_type=orm.object_type,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class RecordingSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, entity: RecordingSessionCreateEntity) -> RecordingSessionEntity:
        stmt = select(RecordingSessionORM).where(
            RecordingSessionORM.unique_key == entity.unique_key
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            orm = RecordingSessionORM(
                unique_key=entity.unique_key,
                minute_token=entity.minute_token,
                meeting_id=entity.meeting_id,
                object_type=entity.object_type,
            )
            self._session.add(orm)
        else:
            if entity.minute_token is not None:
                orm.minute_token = entity.minute_token
            if entity.meeting_id is not None:
                orm.meeting_id = entity.meeting_id
            if entity.object_type is not None:
                orm.object_type = entity.object_type
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def update(self, entity: RecordingSessionUpdateEntity) -> RecordingSessionEntity | None:
        orm = await self._session.get(RecordingSessionORM, entity.id)
        if orm is None:
            return None
        if entity.minute_token is not None:
            orm.minute_token = entity.minute_token
        if entity.meeting_id is not None:
            orm.meeting_id = entity.meeting_id
        if entity.object_type is not None:
            orm.object_type = entity.object_type
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def get_by_unique_key(self, unique_key: str) -> RecordingSessionEntity | None:
        stmt = select(RecordingSessionORM).where(RecordingSessionORM.unique_key == unique_key)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None
