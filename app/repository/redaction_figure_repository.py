from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.redaction_figure import (
    RedactionFigureEntity,
    RedactionFigureUpsertEntity,
)
from app.repository.json_map import dump_json, load_json_list
from app.repository.orm.redaction_figure import RedactionFigureORM


def _to_entity(orm: RedactionFigureORM) -> RedactionFigureEntity:
    return RedactionFigureEntity(
        id=orm.id,
        owner_user_id=orm.owner_user_id,
        minute_token=orm.minute_token,
        figure_id=orm.figure_id,
        sensitive=bool(orm.sensitive),
        status=orm.status,
        abandon_reason=orm.abandon_reason,
        original_relpath=orm.original_relpath,
        asset_relpath=orm.asset_relpath,
        regions=load_json_list(orm.regions_json),
        attempts=load_json_list(orm.attempts_json),
    )


class RedactionFigureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_minute_token(
        self, minute_token: str, *, owner_user_id: int
    ) -> list[RedactionFigureEntity]:
        stmt = (
            select(RedactionFigureORM)
            .where(
                RedactionFigureORM.owner_user_id == owner_user_id,
                RedactionFigureORM.minute_token == minute_token,
            )
            .order_by(RedactionFigureORM.figure_id.asc())
        )
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def replace_for_minute(
        self,
        minute_token: str,
        items: list[RedactionFigureUpsertEntity],
        *,
        owner_user_id: int,
    ) -> list[RedactionFigureEntity]:
        await self._session.execute(
            delete(RedactionFigureORM).where(
                RedactionFigureORM.owner_user_id == owner_user_id,
                RedactionFigureORM.minute_token == minute_token,
            )
        )
        out: list[RedactionFigureEntity] = []
        for item in items:
            orm = RedactionFigureORM(
                owner_user_id=owner_user_id,
                minute_token=minute_token,
                figure_id=item.figure_id,
                sensitive=item.sensitive,
                status=item.status,
                abandon_reason=item.abandon_reason,
                original_relpath=item.original_relpath,
                asset_relpath=item.asset_relpath,
                regions_json=dump_json(item.regions, default="[]"),
                attempts_json=dump_json(item.attempts, default="[]"),
            )
            self._session.add(orm)
            await self._session.flush()
            await self._session.refresh(orm)
            out.append(_to_entity(orm))
        return out

    async def upsert(self, entity: RedactionFigureUpsertEntity) -> RedactionFigureEntity:
        stmt = select(RedactionFigureORM).where(
            RedactionFigureORM.owner_user_id == entity.owner_user_id,
            RedactionFigureORM.minute_token == entity.minute_token,
            RedactionFigureORM.figure_id == entity.figure_id,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            orm = RedactionFigureORM(
                owner_user_id=entity.owner_user_id,
                minute_token=entity.minute_token,
                figure_id=entity.figure_id,
            )
            self._session.add(orm)
        orm.sensitive = entity.sensitive
        orm.status = entity.status
        orm.abandon_reason = entity.abandon_reason
        orm.original_relpath = entity.original_relpath
        orm.asset_relpath = entity.asset_relpath
        orm.regions_json = dump_json(entity.regions, default="[]")
        orm.attempts_json = dump_json(entity.attempts, default="[]")
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)
