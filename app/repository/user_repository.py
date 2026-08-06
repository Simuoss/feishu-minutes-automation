from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.user import (
    UserCreateEntity,
    UserEntity,
    UserQueryEntity,
    UserUpdateEntity,
)
from app.repository.orm.user import UserORM


def _to_entity(orm: UserORM) -> UserEntity:
    return UserEntity(
        id=orm.id,
        username=orm.username,
        password_hash=orm.password_hash,
        status=orm.status,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, entity: UserCreateEntity) -> UserEntity:
        orm = UserORM(
            username=entity.username,
            password_hash=entity.password_hash,
            status=entity.status,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def update(self, entity: UserUpdateEntity) -> UserEntity | None:
        orm = await self._session.get(UserORM, entity.id)
        if orm is None:
            return None
        if entity.username is not None:
            orm.username = entity.username
        if entity.password_hash is not None:
            orm.password_hash = entity.password_hash
        if entity.status is not None:
            orm.status = entity.status
        if entity.updated_at is not None:
            orm.updated_at = entity.updated_at
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def get_by_id(self, user_id: int) -> UserEntity | None:
        orm = await self._session.get(UserORM, user_id)
        return _to_entity(orm) if orm else None

    async def get_by_username(self, username: str) -> UserEntity | None:
        stmt = select(UserORM).where(UserORM.username == username)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def query(self, query: UserQueryEntity) -> list[UserEntity]:
        stmt = select(UserORM)
        if query.id is not None:
            stmt = stmt.where(UserORM.id == query.id)
        if query.username is not None:
            stmt = stmt.where(UserORM.username == query.username)
        if query.status is not None:
            stmt = stmt.where(UserORM.status == query.status)
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def list_all(self) -> list[UserEntity]:
        stmt = select(UserORM).order_by(UserORM.id.asc())
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]
