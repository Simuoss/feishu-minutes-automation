from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.summary_run import SummaryRunEntity, SummaryRunUpsertEntity
from app.repository.json_map import dump_json, load_json_list
from app.repository.orm.summary_run import SummaryRunORM


def _to_entity(orm: SummaryRunORM) -> SummaryRunEntity:
    return SummaryRunEntity(
        id=orm.id,
        owner_user_id=orm.owner_user_id,
        minute_token=orm.minute_token,
        model=orm.model,
        input_tokens=orm.input_tokens,
        output_tokens=orm.output_tokens,
        attempts=orm.attempts,
        elapsed_seconds=orm.elapsed_seconds,
        stop_reason=orm.stop_reason,
        truncated=orm.truncated,
        transcript_chars=orm.transcript_chars,
        summary_chars=orm.summary_chars,
        anchor_total=orm.anchor_total,
        anchor_aligned=orm.anchor_aligned,
        figure_planned=orm.figure_planned,
        figure_for_writing=orm.figure_for_writing,
        figure_used=orm.figure_used,
        figure_scanned=orm.figure_scanned,
        figure_sensitive=orm.figure_sensitive,
        figure_redacted=orm.figure_redacted,
        figure_abandoned=orm.figure_abandoned,
        run_mode=orm.run_mode,
        scene=orm.scene,
        scene_reason=orm.scene_reason,
        generated_at=orm.generated_at,
        redacted_at=orm.redacted_at,
        r2_synced_at=orm.r2_synced_at,
        r2_uploaded=orm.r2_uploaded,
        summary_relpath=orm.summary_relpath,
        anchor_suspicious=load_json_list(orm.anchor_suspicious_json),
        updated_at=orm.updated_at,
    )


def _apply_upsert(orm: SummaryRunORM, entity: SummaryRunUpsertEntity) -> None:
    orm.model = entity.model
    orm.input_tokens = entity.input_tokens
    orm.output_tokens = entity.output_tokens
    orm.attempts = entity.attempts
    orm.elapsed_seconds = entity.elapsed_seconds
    orm.stop_reason = entity.stop_reason
    orm.truncated = entity.truncated
    orm.transcript_chars = entity.transcript_chars
    orm.summary_chars = entity.summary_chars
    orm.anchor_total = entity.anchor_total
    orm.anchor_aligned = entity.anchor_aligned
    orm.figure_planned = entity.figure_planned
    orm.figure_for_writing = entity.figure_for_writing
    orm.figure_used = entity.figure_used
    orm.figure_scanned = entity.figure_scanned
    orm.figure_sensitive = entity.figure_sensitive
    orm.figure_redacted = entity.figure_redacted
    orm.figure_abandoned = entity.figure_abandoned
    orm.run_mode = entity.run_mode
    orm.scene = entity.scene
    orm.scene_reason = entity.scene_reason
    orm.generated_at = entity.generated_at
    orm.redacted_at = entity.redacted_at
    orm.r2_synced_at = entity.r2_synced_at
    orm.r2_uploaded = entity.r2_uploaded
    orm.summary_relpath = entity.summary_relpath
    orm.anchor_suspicious_json = dump_json(entity.anchor_suspicious, default="[]")
    orm.updated_at = entity.updated_at


class SummaryRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_minute_token(
        self, minute_token: str, *, owner_user_id: int
    ) -> SummaryRunEntity | None:
        stmt = select(SummaryRunORM).where(
            SummaryRunORM.owner_user_id == owner_user_id,
            SummaryRunORM.minute_token == minute_token,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def map_by_minute_tokens(
        self, minute_tokens: list[str], *, owner_user_id: int
    ) -> dict[str, SummaryRunEntity]:
        if not minute_tokens:
            return {}
        stmt = select(SummaryRunORM).where(
            SummaryRunORM.owner_user_id == owner_user_id,
            SummaryRunORM.minute_token.in_(minute_tokens),
        )
        result = await self._session.execute(stmt)
        return {orm.minute_token: _to_entity(orm) for orm in result.scalars().all()}

    async def upsert(self, entity: SummaryRunUpsertEntity) -> SummaryRunEntity:
        stmt = select(SummaryRunORM).where(
            SummaryRunORM.owner_user_id == entity.owner_user_id,
            SummaryRunORM.minute_token == entity.minute_token,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            orm = SummaryRunORM(
                owner_user_id=entity.owner_user_id,
                minute_token=entity.minute_token,
            )
            self._session.add(orm)
        _apply_upsert(orm, entity)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)
