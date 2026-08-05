from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.meeting_record import (
    MeetingRecordCreateEntity,
    MeetingRecordEntity,
    MeetingRecordQueryEntity,
    MeetingRecordUpdateEntity,
)
from app.repository.orm.meeting_record import MeetingRecordORM


def _to_entity(orm: MeetingRecordORM) -> MeetingRecordEntity:
    return MeetingRecordEntity(
        id=orm.id,
        feishu_event_id=orm.feishu_event_id,
        event_type=orm.event_type,
        unique_key=orm.unique_key,
        minute_token=orm.minute_token,
        meeting_id=orm.meeting_id,
        title=orm.title,
        duration_ms=orm.duration_ms,
        has_video=orm.has_video,
        status=orm.status,
        error_message=orm.error_message,
        storage_path=orm.storage_path,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class MeetingRecordRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, entity: MeetingRecordCreateEntity) -> MeetingRecordEntity:
        orm = MeetingRecordORM(
            feishu_event_id=entity.feishu_event_id,
            event_type=entity.event_type,
            unique_key=entity.unique_key,
            minute_token=entity.minute_token,
            meeting_id=entity.meeting_id,
            title=entity.title,
            duration_ms=entity.duration_ms,
            has_video=entity.has_video,
            status=entity.status,
            error_message=entity.error_message,
            storage_path=entity.storage_path,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def update(self, entity: MeetingRecordUpdateEntity) -> MeetingRecordEntity | None:
        orm = await self._session.get(MeetingRecordORM, entity.id)
        if orm is None:
            return None
        if entity.minute_token is not None:
            orm.minute_token = entity.minute_token
        if entity.meeting_id is not None:
            orm.meeting_id = entity.meeting_id
        if entity.title is not None:
            orm.title = entity.title
        if entity.duration_ms is not None:
            orm.duration_ms = entity.duration_ms
        if entity.has_video is not None:
            orm.has_video = entity.has_video
        if entity.status is not None:
            orm.status = entity.status
        if entity.error_message is not None:
            orm.error_message = entity.error_message
        if entity.storage_path is not None:
            orm.storage_path = entity.storage_path
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def get_by_id(self, record_id: int) -> MeetingRecordEntity | None:
        orm = await self._session.get(MeetingRecordORM, record_id)
        return _to_entity(orm) if orm else None

    async def get_by_event_id(self, event_id: str) -> MeetingRecordEntity | None:
        stmt = select(MeetingRecordORM).where(MeetingRecordORM.feishu_event_id == event_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def get_latest_by_minute_token(
        self, minute_token: str
    ) -> MeetingRecordEntity | None:
        stmt = (
            select(MeetingRecordORM)
            .where(MeetingRecordORM.minute_token == minute_token)
            .order_by(MeetingRecordORM.id.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def map_latest_by_minute_tokens(
        self, minute_tokens: list[str]
    ) -> dict[str, MeetingRecordEntity]:
        if not minute_tokens:
            return {}
        stmt = (
            select(MeetingRecordORM)
            .where(MeetingRecordORM.minute_token.in_(minute_tokens))
            .order_by(MeetingRecordORM.id.desc())
        )
        result = await self._session.execute(stmt)
        mapping: dict[str, MeetingRecordEntity] = {}
        for orm in result.scalars().all():
            if orm.minute_token and orm.minute_token not in mapping:
                mapping[orm.minute_token] = _to_entity(orm)
        return mapping

    async def query(self, query: MeetingRecordQueryEntity) -> list[MeetingRecordEntity]:
        stmt = select(MeetingRecordORM)
        if query.id is not None:
            stmt = stmt.where(MeetingRecordORM.id == query.id)
        if query.feishu_event_id is not None:
            stmt = stmt.where(MeetingRecordORM.feishu_event_id == query.feishu_event_id)
        if query.minute_token is not None:
            stmt = stmt.where(MeetingRecordORM.minute_token == query.minute_token)
        if query.unique_key is not None:
            stmt = stmt.where(MeetingRecordORM.unique_key == query.unique_key)
        if query.status is not None:
            stmt = stmt.where(MeetingRecordORM.status == query.status)
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]
