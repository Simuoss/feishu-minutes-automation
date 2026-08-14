from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.pipeline_job import (
    PipelineJobCreateEntity,
    PipelineJobEntity,
    PipelineJobUpdateEntity,
)
from app.repository.orm.pipeline_job import PipelineJobORM


def _to_entity(orm: PipelineJobORM) -> PipelineJobEntity:
    return PipelineJobEntity(
        id=orm.id,
        owner_user_id=orm.owner_user_id,
        minute_token=orm.minute_token,
        job_type=orm.job_type,
        mode=orm.mode,
        status=orm.status,
        stage=orm.stage,
        percent=orm.percent,
        attempt=orm.attempt,
        max_attempts=orm.max_attempts,
        error_message=orm.error_message,
        started_at=orm.started_at,
        finished_at=orm.finished_at,
        broker_updated_at=orm.broker_updated_at,
    )


class PipelineJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, entity: PipelineJobCreateEntity) -> PipelineJobEntity:
        orm = PipelineJobORM(
            owner_user_id=entity.owner_user_id,
            minute_token=entity.minute_token,
            job_type=entity.job_type,
            mode=entity.mode,
            status=entity.status,
            stage=entity.stage,
            percent=entity.percent,
            attempt=entity.attempt,
            max_attempts=entity.max_attempts,
            error_message=entity.error_message,
            started_at=entity.started_at,
            finished_at=entity.finished_at,
            broker_updated_at=entity.broker_updated_at,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def update(self, entity: PipelineJobUpdateEntity) -> PipelineJobEntity | None:
        orm = await self._session.get(PipelineJobORM, entity.id)
        if orm is None:
            return None
        for field in (
            "status",
            "stage",
            "percent",
            "attempt",
            "max_attempts",
            "error_message",
            "started_at",
            "finished_at",
            "broker_updated_at",
            "mode",
        ):
            value = getattr(entity, field)
            if value is not None:
                setattr(orm, field, value)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def get_latest(
        self,
        minute_token: str,
        job_type: str | None = None,
        *,
        owner_user_id: int,
    ) -> PipelineJobEntity | None:
        stmt = (
            select(PipelineJobORM)
            .where(
                PipelineJobORM.owner_user_id == owner_user_id,
                PipelineJobORM.minute_token == minute_token,
            )
            .order_by(PipelineJobORM.id.desc())
            .limit(1)
        )
        if job_type is not None:
            stmt = stmt.where(PipelineJobORM.job_type == job_type)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def list_by_minute_token(
        self, minute_token: str, *, owner_user_id: int
    ) -> list[PipelineJobEntity]:
        stmt = (
            select(PipelineJobORM)
            .where(
                PipelineJobORM.owner_user_id == owner_user_id,
                PipelineJobORM.minute_token == minute_token,
            )
            .order_by(PipelineJobORM.id.desc())
        )
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def list_by_statuses(
        self,
        statuses: list[str],
        *,
        job_types: list[str] | None = None,
    ) -> list[PipelineJobEntity]:
        stmt = (
            select(PipelineJobORM)
            .where(PipelineJobORM.status.in_(statuses))
            .order_by(PipelineJobORM.id.asc())
        )
        if job_types:
            stmt = stmt.where(PipelineJobORM.job_type.in_(job_types))
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def claim(self, job_id: int) -> PipelineJobEntity | None:
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        stmt = (
            update(PipelineJobORM)
            .where(
                PipelineJobORM.id == job_id,
                PipelineJobORM.status == "QUEUED",
            )
            .values(
                status="RUNNING",
                stage="START",
                started_at=now,
                broker_updated_at=now,
            )
        )
        result = await self._session.execute(stmt)
        if int(result.rowcount or 0) == 0:
            return None
        await self._session.flush()
        orm = await self._session.get(PipelineJobORM, job_id)
        return _to_entity(orm) if orm else None
