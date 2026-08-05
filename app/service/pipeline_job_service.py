"""下载 / 纪要流水线状态落库（SSE 仍走内存 broker）。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.async_bridge import schedule_async
from app.data_model.entity.pipeline_job import (
    PipelineJobCreateEntity,
    PipelineJobUpdateEntity,
)
from app.repository.uow import UnitOfWork
from app.service.metadata_db_service import set_summary_status

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


async def start_job(
    minute_token: str,
    *,
    job_type: str,
    mode: str | None = None,
    stage: str | None = None,
    attempt: int | None = None,
    max_attempts: int | None = None,
) -> int | None:
    now = _now_ms()
    async with UnitOfWork() as uow:
        assert uow.pipeline_jobs is not None
        entity = await uow.pipeline_jobs.create(
            PipelineJobCreateEntity(
                minute_token=minute_token,
                job_type=job_type.upper(),
                mode=(mode or "").upper() or None,
                status="RUNNING",
                stage=stage,
                percent=0.0,
                attempt=attempt,
                max_attempts=max_attempts,
                started_at=now,
                broker_updated_at=now,
            )
        )
        await uow.commit()
        job_id = entity.id
    if job_type.upper() == "SUMMARY":
        await set_summary_status(minute_token, "GENERATING")
    return job_id


async def update_job(
    job_id: int | None,
    *,
    status: str | None = None,
    stage: str | None = None,
    percent: float | None = None,
    error_message: str | None = None,
    finished: bool = False,
) -> None:
    if job_id is None:
        return
    now = _now_ms()
    async with UnitOfWork() as uow:
        assert uow.pipeline_jobs is not None
        await uow.pipeline_jobs.update(
            PipelineJobUpdateEntity(
                id=job_id,
                status=status.upper() if status else None,
                stage=stage,
                percent=percent,
                error_message=error_message,
                finished_at=now if finished else None,
                broker_updated_at=now,
            )
        )
        await uow.commit()


def schedule_start_job(
    minute_token: str,
    *,
    job_type: str,
    mode: str | None = None,
    stage: str | None = None,
) -> None:
    schedule_async(
        start_job(minute_token, job_type=job_type, mode=mode, stage=stage),
        what="pipeline_job_start",
    )


def schedule_update_job(
    job_id: int | None,
    *,
    status: str | None = None,
    stage: str | None = None,
    percent: float | None = None,
    error_message: str | None = None,
    finished: bool = False,
) -> None:
    if job_id is None:
        return
    schedule_async(
        update_job(
            job_id,
            status=status,
            stage=stage,
            percent=percent,
            error_message=error_message,
            finished=finished,
        ),
        what="pipeline_job_update",
    )
