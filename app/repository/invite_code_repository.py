from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.invite_code import (
    InviteCodeCreateEntity,
    InviteCodeEntity,
    InviteCodeQueryEntity,
)
from app.repository.orm.invite_code import InviteCodeORM


def _to_entity(orm: InviteCodeORM) -> InviteCodeEntity:
    return InviteCodeEntity(
        id=orm.id,
        code=orm.code,
        created_by_user_id=orm.created_by_user_id,
        created_at=orm.created_at,
        used_by_user_id=orm.used_by_user_id,
        used_at=orm.used_at,
        status=orm.status,
    )


class InviteCodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, entity: InviteCodeCreateEntity) -> InviteCodeEntity:
        orm = InviteCodeORM(
            code=entity.code,
            created_by_user_id=entity.created_by_user_id,
            created_at=entity.created_at,
            used_by_user_id=None,
            used_at=None,
            status=entity.status,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def get_by_code(self, code: str) -> InviteCodeEntity | None:
        stmt = select(InviteCodeORM).where(InviteCodeORM.code == code)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def query(self, query: InviteCodeQueryEntity) -> list[InviteCodeEntity]:
        stmt = select(InviteCodeORM).order_by(InviteCodeORM.id.desc())
        if query.created_by_user_id is not None:
            stmt = stmt.where(
                InviteCodeORM.created_by_user_id == query.created_by_user_id
            )
        if query.status is not None:
            stmt = stmt.where(InviteCodeORM.status == query.status)
        if query.code is not None:
            stmt = stmt.where(InviteCodeORM.code == query.code)
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def consume_active(
        self, *, code: str, used_by_user_id: int, used_at: int
    ) -> bool:
        """原子消费：仅 ACTIVE 可变为 USED，防并发双花。"""
        stmt = (
            update(InviteCodeORM)
            .where(
                InviteCodeORM.code == code,
                InviteCodeORM.status == "ACTIVE",
            )
            .values(
                status="USED",
                used_by_user_id=used_by_user_id,
                used_at=used_at,
            )
        )
        result = await self._session.execute(stmt)
        return bool(result.rowcount)

    async def revoke(self, *, code: str, created_by_user_id: int) -> bool:
        stmt = (
            update(InviteCodeORM)
            .where(
                InviteCodeORM.code == code,
                InviteCodeORM.created_by_user_id == created_by_user_id,
                InviteCodeORM.status == "ACTIVE",
            )
            .values(status="REVOKED")
        )
        result = await self._session.execute(stmt)
        return bool(result.rowcount)
