from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.feishu_user_token import (
    FeishuUserTokenEntity,
    FeishuUserTokenUpsertEntity,
)
from app.repository.orm.feishu_user_token import FeishuUserTokenORM


def _to_entity(orm: FeishuUserTokenORM) -> FeishuUserTokenEntity:
    return FeishuUserTokenEntity(
        id=orm.id,
        user_id=orm.user_id,
        access_token=orm.access_token,
        refresh_token=orm.refresh_token,
        expires_at=orm.expires_at,
        refresh_expires_at=orm.refresh_expires_at,
        scope=orm.scope,
        updated_at=orm.updated_at,
    )


class FeishuUserTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_user_id(self, user_id: int) -> FeishuUserTokenEntity | None:
        stmt = select(FeishuUserTokenORM).where(FeishuUserTokenORM.user_id == user_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def list_user_ids_with_tokens(self) -> list[int]:
        """返回已保存飞书 Token 的用户 id（用于事件回调按用户隔离拉取）。"""
        stmt = select(FeishuUserTokenORM.user_id).where(
            (FeishuUserTokenORM.access_token.is_not(None))
            | (FeishuUserTokenORM.refresh_token.is_not(None))
        )
        result = await self._session.execute(stmt)
        return [int(uid) for uid in result.scalars().all() if uid is not None]

    async def upsert(self, entity: FeishuUserTokenUpsertEntity) -> FeishuUserTokenEntity:
        stmt = select(FeishuUserTokenORM).where(
            FeishuUserTokenORM.user_id == entity.user_id
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            orm = FeishuUserTokenORM(user_id=entity.user_id)
            self._session.add(orm)
        orm.access_token = entity.access_token
        orm.refresh_token = entity.refresh_token
        orm.expires_at = entity.expires_at
        orm.refresh_expires_at = entity.refresh_expires_at
        orm.scope = entity.scope
        orm.updated_at = entity.updated_at
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def delete_by_user_id(self, user_id: int) -> None:
        await self._session.execute(
            delete(FeishuUserTokenORM).where(FeishuUserTokenORM.user_id == user_id)
        )
        await self._session.flush()
