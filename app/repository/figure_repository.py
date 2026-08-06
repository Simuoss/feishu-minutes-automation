from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.figure import FigureEntity, FigureUpsertEntity
from app.repository.orm.figure import FigureORM


def _to_entity(orm: FigureORM) -> FigureEntity:
    return FigureEntity(
        id=orm.id,
        owner_user_id=orm.owner_user_id,
        minute_token=orm.minute_token,
        figure_id=orm.figure_id,
        relative_path=orm.relative_path,
        timestamp=orm.timestamp,
        frame_seconds=orm.frame_seconds,
        expect=orm.expect,
        purpose=orm.purpose,
        status=orm.status,
    )


class FigureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_minute_token(
        self, minute_token: str, *, owner_user_id: int
    ) -> list[FigureEntity]:
        stmt = (
            select(FigureORM)
            .where(
                FigureORM.owner_user_id == owner_user_id,
                FigureORM.minute_token == minute_token,
            )
            .order_by(FigureORM.figure_id.asc())
        )
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def replace_for_minute(
        self,
        minute_token: str,
        figures: list[FigureUpsertEntity],
        *,
        owner_user_id: int,
    ) -> list[FigureEntity]:
        await self._session.execute(
            delete(FigureORM).where(
                FigureORM.owner_user_id == owner_user_id,
                FigureORM.minute_token == minute_token,
            )
        )
        out: list[FigureEntity] = []
        for item in figures:
            orm = FigureORM(
                owner_user_id=owner_user_id,
                minute_token=minute_token,
                figure_id=item.figure_id,
                relative_path=item.relative_path,
                timestamp=item.timestamp,
                frame_seconds=item.frame_seconds,
                expect=item.expect,
                purpose=item.purpose,
                status=item.status,
            )
            self._session.add(orm)
            await self._session.flush()
            await self._session.refresh(orm)
            out.append(_to_entity(orm))
        return out

    async def upsert(self, entity: FigureUpsertEntity) -> FigureEntity:
        stmt = select(FigureORM).where(
            FigureORM.owner_user_id == entity.owner_user_id,
            FigureORM.minute_token == entity.minute_token,
            FigureORM.figure_id == entity.figure_id,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            orm = FigureORM(
                owner_user_id=entity.owner_user_id,
                minute_token=entity.minute_token,
                figure_id=entity.figure_id,
            )
            self._session.add(orm)
        orm.relative_path = entity.relative_path
        orm.timestamp = entity.timestamp
        orm.frame_seconds = entity.frame_seconds
        orm.expect = entity.expect
        orm.purpose = entity.purpose
        orm.status = entity.status
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)
