from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.share_access_log import (
    ShareAccessLogCreateEntity,
    ShareAccessLogEntity,
)
from app.repository.orm.share_access_log import ShareAccessLogORM


def _to_entity(orm: ShareAccessLogORM) -> ShareAccessLogEntity:
    return ShareAccessLogEntity(
        id=orm.id,
        access_key_id=orm.access_key_id,
        share_id=orm.share_id,
        minute_token=orm.minute_token,
        action=orm.action,
        ip=orm.ip,
        user_agent=orm.user_agent,
        created_at=orm.created_at,
    )


class ShareAccessLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, entity: ShareAccessLogCreateEntity) -> ShareAccessLogEntity:
        orm = ShareAccessLogORM(
            access_key_id=entity.access_key_id,
            share_id=entity.share_id,
            minute_token=entity.minute_token,
            action=entity.action,
            ip=entity.ip,
            user_agent=entity.user_agent,
            created_at=entity.created_at,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def list_by_key(
        self, access_key_id: int, *, limit: int = 100, offset: int = 0
    ) -> list[ShareAccessLogEntity]:
        stmt = (
            select(ShareAccessLogORM)
            .where(ShareAccessLogORM.access_key_id == access_key_id)
            .order_by(ShareAccessLogORM.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]
