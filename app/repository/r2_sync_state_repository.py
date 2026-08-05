from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.r2_sync_state import R2SyncStateEntity, R2SyncStateUpsertEntity
from app.repository.json_map import dump_json, load_json_dict
from app.repository.orm.r2_sync_state import R2SyncStateORM


def _to_entity(orm: R2SyncStateORM) -> R2SyncStateEntity:
    return R2SyncStateEntity(
        id=orm.id,
        minute_token=orm.minute_token,
        video_key=orm.video_key,
        video_etag=orm.video_etag,
        video_status=orm.video_status,
        video_updated_at=orm.video_updated_at,
        assets=load_json_dict(orm.assets_json),
        updated_at=orm.updated_at,
    )


class R2SyncStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_minute_token(self, minute_token: str) -> R2SyncStateEntity | None:
        stmt = select(R2SyncStateORM).where(R2SyncStateORM.minute_token == minute_token)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def upsert(self, entity: R2SyncStateUpsertEntity) -> R2SyncStateEntity:
        stmt = select(R2SyncStateORM).where(
            R2SyncStateORM.minute_token == entity.minute_token
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            orm = R2SyncStateORM(minute_token=entity.minute_token)
            self._session.add(orm)
        orm.video_key = entity.video_key
        orm.video_etag = entity.video_etag
        orm.video_status = entity.video_status
        orm.video_updated_at = entity.video_updated_at
        orm.assets_json = dump_json(entity.assets, default="{}")
        orm.updated_at = entity.updated_at
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)
