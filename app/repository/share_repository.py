from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.share import ShareAdminQueryEntity, ShareCreateEntity, ShareEntity
from app.repository.orm.share import ShareORM


def _to_entity(orm: ShareORM) -> ShareEntity:
    return ShareEntity(
        id=orm.id,
        share_token=orm.share_token,
        minute_token=orm.minute_token,
        access_mode=orm.access_mode,
        allow_export=orm.allow_export,
        access_key_id=orm.access_key_id,
        owner_user_id=orm.owner_user_id,
        created_at=orm.created_at,
        revoked_at=orm.revoked_at,
    )


class ShareRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, entity: ShareCreateEntity) -> ShareEntity:
        orm = ShareORM(
            share_token=entity.share_token,
            minute_token=entity.minute_token,
            access_mode=entity.access_mode,
            allow_export=entity.allow_export,
            access_key_id=entity.access_key_id,
            owner_user_id=entity.owner_user_id,
            created_at=entity.created_at,
            revoked_at=None,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def get_by_id(self, share_id: int) -> ShareEntity | None:
        orm = await self._session.get(ShareORM, share_id)
        return _to_entity(orm) if orm else None

    async def get_by_token(self, share_token: str) -> ShareEntity | None:
        stmt = select(ShareORM).where(ShareORM.share_token == share_token)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def list_by_minute(
        self,
        minute_token: str,
        include_revoked: bool = False,
        *,
        owner_user_id: int | None = None,
    ) -> list[ShareEntity]:
        stmt = (
            select(ShareORM)
            .where(ShareORM.minute_token == minute_token)
            .order_by(ShareORM.id.desc())
        )
        if not include_revoked:
            stmt = stmt.where(ShareORM.revoked_at.is_(None))
        if owner_user_id is not None:
            stmt = stmt.where(ShareORM.owner_user_id == owner_user_id)
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def list_active_by_minutes(self, minute_tokens: list[str]) -> list[ShareEntity]:
        if not minute_tokens:
            return []
        stmt = (
            select(ShareORM)
            .where(
                ShareORM.minute_token.in_(minute_tokens),
                ShareORM.revoked_at.is_(None),
            )
            .order_by(ShareORM.id.desc())
        )
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def list_all(
        self,
        query: ShareAdminQueryEntity | None = None,
        *,
        include_revoked: bool = False,
        minute_token: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[ShareEntity]:
        q = query or ShareAdminQueryEntity(
            status="ALL" if include_revoked else "ACTIVE",
            minute_token=minute_token,
            limit=limit,
            offset=offset,
        )
        stmt = select(ShareORM)
        status = (q.status or "ACTIVE").upper()
        if status == "ACTIVE":
            stmt = stmt.where(ShareORM.revoked_at.is_(None))
        elif status == "REVOKED":
            stmt = stmt.where(ShareORM.revoked_at.is_not(None))
        if q.minute_token:
            stmt = stmt.where(ShareORM.minute_token == q.minute_token.strip())
        if q.access_mode:
            stmt = stmt.where(ShareORM.access_mode == q.access_mode.upper())
        if q.allow_export is not None:
            stmt = stmt.where(ShareORM.allow_export.is_(bool(q.allow_export)))
        if q.no_access_key:
            stmt = stmt.where(ShareORM.access_key_id.is_(None))
        elif q.access_key_id is not None:
            stmt = stmt.where(ShareORM.access_key_id == q.access_key_id)
        if q.created_from is not None:
            stmt = stmt.where(ShareORM.created_at >= q.created_from)
        if q.created_to is not None:
            stmt = stmt.where(ShareORM.created_at <= q.created_to)
        if q.owner_user_id is not None:
            stmt = stmt.where(ShareORM.owner_user_id == q.owner_user_id)

        # 管理端综合筛选含会议名（本地 meta），统一拉候选后由 Service 排序分页
        stmt = stmt.order_by(ShareORM.created_at.desc(), ShareORM.id.desc()).limit(2000)
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def list_by_ids(self, share_ids: list[int]) -> list[ShareEntity]:
        if not share_ids:
            return []
        stmt = select(ShareORM).where(ShareORM.id.in_(share_ids))
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def list_active_by_access_key_ids(self, key_ids: list[int]) -> list[ShareEntity]:
        if not key_ids:
            return []
        stmt = (
            select(ShareORM)
            .where(
                ShareORM.access_key_id.in_(key_ids),
                ShareORM.revoked_at.is_(None),
            )
            .order_by(ShareORM.created_at.desc(), ShareORM.id.desc())
        )
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def update_fields(
        self,
        share_id: int,
        *,
        access_mode: str | None = None,
        allow_export: bool | None = None,
        access_key_id: int | None = None,
        clear_access_key: bool = False,
    ) -> ShareEntity | None:
        orm = await self._session.get(ShareORM, share_id)
        if orm is None:
            return None
        if access_mode is not None:
            orm.access_mode = access_mode
        if allow_export is not None:
            orm.allow_export = allow_export
        if clear_access_key:
            orm.access_key_id = None
        elif access_key_id is not None:
            orm.access_key_id = access_key_id
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def revoke(self, share_id: int, revoked_at: int) -> ShareEntity | None:
        orm = await self._session.get(ShareORM, share_id)
        if orm is None:
            return None
        orm.revoked_at = revoked_at
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def backfill_null_owner(self, owner_user_id: int) -> int:
        stmt = select(ShareORM).where(ShareORM.owner_user_id.is_(None))
        result = await self._session.execute(stmt)
        count = 0
        for orm in result.scalars().all():
            orm.owner_user_id = owner_user_id
            count += 1
        if count:
            await self._session.flush()
        return count
