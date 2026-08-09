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
        password_hash=orm.password_hash or "",
        status=orm.status,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
        display_name=getattr(orm, "display_name", None),
        display_name_set_at=getattr(orm, "display_name_set_at", None),
        feishu_open_id=getattr(orm, "feishu_open_id", None),
        feishu_union_id=getattr(orm, "feishu_union_id", None),
        feishu_user_id=getattr(orm, "feishu_user_id", None),
        feishu_name=getattr(orm, "feishu_name", None),
        feishu_en_name=getattr(orm, "feishu_en_name", None),
        feishu_avatar_url=getattr(orm, "feishu_avatar_url", None),
        feishu_tenant_key=getattr(orm, "feishu_tenant_key", None),
        feishu_email=getattr(orm, "feishu_email", None),
        feishu_mobile=getattr(orm, "feishu_mobile", None),
        feishu_profile_json=getattr(orm, "feishu_profile_json", None),
    )


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, entity: UserCreateEntity) -> UserEntity:
        orm = UserORM(
            username=entity.username,
            password_hash=entity.password_hash or "",
            status=entity.status,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            display_name=entity.display_name,
            display_name_set_at=entity.display_name_set_at,
            feishu_open_id=entity.feishu_open_id,
            feishu_union_id=entity.feishu_union_id,
            feishu_user_id=entity.feishu_user_id,
            feishu_name=entity.feishu_name,
            feishu_en_name=entity.feishu_en_name,
            feishu_avatar_url=entity.feishu_avatar_url,
            feishu_tenant_key=entity.feishu_tenant_key,
            feishu_email=entity.feishu_email,
            feishu_mobile=entity.feishu_mobile,
            feishu_profile_json=entity.feishu_profile_json,
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
        if entity.display_name is not None:
            orm.display_name = entity.display_name
        if entity.display_name_set_at is not None:
            orm.display_name_set_at = entity.display_name_set_at
        if entity.feishu_open_id is not None:
            orm.feishu_open_id = entity.feishu_open_id
        if entity.feishu_union_id is not None:
            orm.feishu_union_id = entity.feishu_union_id
        if entity.feishu_user_id is not None:
            orm.feishu_user_id = entity.feishu_user_id
        if entity.feishu_name is not None:
            orm.feishu_name = entity.feishu_name
        if entity.feishu_en_name is not None:
            orm.feishu_en_name = entity.feishu_en_name
        if entity.feishu_avatar_url is not None:
            orm.feishu_avatar_url = entity.feishu_avatar_url
        if entity.feishu_tenant_key is not None:
            orm.feishu_tenant_key = entity.feishu_tenant_key
        if entity.feishu_email is not None:
            orm.feishu_email = entity.feishu_email
        if entity.feishu_mobile is not None:
            orm.feishu_mobile = entity.feishu_mobile
        if entity.feishu_profile_json is not None:
            orm.feishu_profile_json = entity.feishu_profile_json
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

    async def get_by_feishu_open_id(self, open_id: str) -> UserEntity | None:
        if not open_id:
            return None
        stmt = select(UserORM).where(UserORM.feishu_open_id == open_id)
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
        if query.feishu_open_id is not None:
            stmt = stmt.where(UserORM.feishu_open_id == query.feishu_open_id)
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def list_all(self) -> list[UserEntity]:
        stmt = select(UserORM).order_by(UserORM.id.asc())
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]
