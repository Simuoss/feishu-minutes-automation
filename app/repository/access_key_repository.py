from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.access_key import (
    AccessKeyCreateEntity,
    AccessKeyEntity,
    AccessKeyQueryEntity,
)
from app.repository.orm.access_key import AccessKeyORM


def _to_entity(orm: AccessKeyORM) -> AccessKeyEntity:
    return AccessKeyEntity(
        id=orm.id,
        name=orm.name,
        key_hash=orm.key_hash,
        key_prefix=orm.key_prefix,
        expires_at=orm.expires_at,
        created_at=orm.created_at,
        revoked_at=orm.revoked_at,
    )


class AccessKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, entity: AccessKeyCreateEntity) -> AccessKeyEntity:
        orm = AccessKeyORM(
            name=entity.name,
            key_hash=entity.key_hash,
            key_prefix=entity.key_prefix,
            expires_at=entity.expires_at,
            created_at=entity.created_at,
            revoked_at=None,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def get_by_id(self, key_id: int) -> AccessKeyEntity | None:
        orm = await self._session.get(AccessKeyORM, key_id)
        return _to_entity(orm) if orm else None

    async def get_by_hash(self, key_hash: str) -> AccessKeyEntity | None:
        stmt = select(AccessKeyORM).where(AccessKeyORM.key_hash == key_hash)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def list_by_hashes(self, key_hashes: list[str]) -> list[AccessKeyEntity]:
        if not key_hashes:
            return []
        stmt = select(AccessKeyORM).where(AccessKeyORM.key_hash.in_(key_hashes))
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def list_all(self, query: AccessKeyQueryEntity | None = None) -> list[AccessKeyEntity]:
        query = query or AccessKeyQueryEntity()
        stmt = select(AccessKeyORM).order_by(AccessKeyORM.id.desc())
        if query.id is not None:
            stmt = stmt.where(AccessKeyORM.id == query.id)
        if not query.include_revoked:
            stmt = stmt.where(AccessKeyORM.revoked_at.is_(None))
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def revoke(self, key_id: int, revoked_at: int) -> AccessKeyEntity | None:
        orm = await self._session.get(AccessKeyORM, key_id)
        if orm is None:
            return None
        orm.revoked_at = revoked_at
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)
