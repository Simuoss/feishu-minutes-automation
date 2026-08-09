from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.system_config import (
    SystemConfigCreateEntity,
    SystemConfigEntity,
    SystemConfigUpdateEntity,
)
from app.repository.orm.system_config import SystemConfigORM


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _to_entity(orm: SystemConfigORM) -> SystemConfigEntity:
    return SystemConfigEntity(
        key=orm.key,
        description=orm.description or "",
        value=orm.value or "",
        remark=orm.remark or "",
        updated_at=int(orm.updated_at or 0),
    )


class SystemConfigRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self) -> list[SystemConfigEntity]:
        stmt = select(SystemConfigORM).order_by(SystemConfigORM.key.asc())
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def get(self, key: str) -> SystemConfigEntity | None:
        orm = await self._session.get(SystemConfigORM, key)
        return _to_entity(orm) if orm else None

    async def upsert(self, entity: SystemConfigCreateEntity) -> SystemConfigEntity:
        orm = await self._session.get(SystemConfigORM, entity.key)
        now = _now_ms()
        if orm is None:
            orm = SystemConfigORM(
                key=entity.key,
                description=entity.description,
                value=entity.value,
                remark=entity.remark,
                updated_at=now,
            )
            self._session.add(orm)
        else:
            orm.description = entity.description
            orm.value = entity.value
            orm.remark = entity.remark
            orm.updated_at = now
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def update(self, entity: SystemConfigUpdateEntity) -> SystemConfigEntity | None:
        orm = await self._session.get(SystemConfigORM, entity.key)
        if orm is None:
            return None
        if entity.description is not None:
            orm.description = entity.description
        if entity.value is not None:
            orm.value = entity.value
        if entity.remark is not None:
            orm.remark = entity.remark
        orm.updated_at = _now_ms()
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def ensure_defaults(
        self, defaults: list[SystemConfigCreateEntity]
    ) -> list[SystemConfigEntity]:
        """仅插入缺失的 key，不覆盖超管已改的值。"""
        for item in defaults:
            existing = await self._session.get(SystemConfigORM, item.key)
            if existing is not None:
                continue
            self._session.add(
                SystemConfigORM(
                    key=item.key,
                    description=item.description,
                    value=item.value,
                    remark=item.remark,
                    updated_at=_now_ms(),
                )
            )
        await self._session.flush()
        return await self.list_all()
