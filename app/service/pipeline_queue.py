"""流水线作业入队 / 认领。SUMMARY、SHARE_VIDEO 与 TRANSCRIBE 经此落库，由 worker 消费。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.data_model.entity.pipeline_job import (
    PipelineJobCreateEntity,
    PipelineJobEntity,
    PipelineJobUpdateEntity,
)
from app.repository.uow import UnitOfWork
from app.service.metadata_db_service import set_summary_status

logger = logging.getLogger(__name__)

JOB_SUMMARY = "SUMMARY"
JOB_SHARE_VIDEO = "SHARE_VIDEO"
JOB_TRANSCRIBE = "TRANSCRIBE"
STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"

WORKER_JOB_TYPES = [JOB_SUMMARY, JOB_SHARE_VIDEO, JOB_TRANSCRIBE]

DEFAULT_MAX_ATTEMPTS = 3
MAX_RUNNING_SHARE_VIDEO = 1
# 转写要抽音轨、切片、跑 onnx，都吃 CPU，同时只跑一个
MAX_RUNNING_TRANSCRIBE = 1
# 一场几小时的录音分段送识别，留足余量
TRANSCRIBE_STALE_MS = int(3 * 3600 * 1000)

_wakeup = asyncio.Event()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def notify_worker() -> None:
    try:
        _wakeup.set()
    except RuntimeError:
        return


async def wait_for_work(*, timeout: float) -> None:
    try:
        await asyncio.wait_for(_wakeup.wait(), timeout=timeout)
    except TimeoutError:
        return
    finally:
        _wakeup.clear()


def parse_job_mode(raw: str | None) -> tuple[str, bool]:
    text = (raw or "FULL").strip()
    force = False
    if text.endswith("|force"):
        force = True
        text = text[: -len("|force")]
    return (text or "FULL").upper(), force


def encode_job_mode(mode: str, *, force: bool) -> str:
    base = (mode or "FULL").strip().upper() or "FULL"
    return f"{base}|force" if force else base


def pick_claimable(
    queued: list[PipelineJobEntity],
    running: list[PipelineJobEntity],
    *,
    max_share_video: int = MAX_RUNNING_SHARE_VIDEO,
    max_transcribe: int = MAX_RUNNING_TRANSCRIBE,
) -> PipelineJobEntity | None:
    """纯函数：在已查出的队列里挑下一个可跑的作业。"""
    share_running = sum(1 for j in running if j.job_type == JOB_SHARE_VIDEO)
    transcribe_running = sum(1 for j in running if j.job_type == JOB_TRANSCRIBE)
    busy_summary_owners = {
        j.owner_user_id for j in running if j.job_type == JOB_SUMMARY
    }
    for job in queued:
        if job.job_type == JOB_SUMMARY and job.owner_user_id in busy_summary_owners:
            continue
        if job.job_type == JOB_SHARE_VIDEO and share_running >= max_share_video:
            continue
        if job.job_type == JOB_TRANSCRIBE and transcribe_running >= max_transcribe:
            continue
        return job
    return None


async def enqueue_job(
    minute_token: str,
    *,
    owner_user_id: int,
    job_type: str,
    mode: str | None = None,
    force: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> int | None:
    job_type = job_type.upper()
    now = _now_ms()
    encoded_mode = (
        encode_job_mode(mode or "FULL", force=force)
        if job_type == JOB_SUMMARY
        else (mode.upper() if mode else None)
    )
    async with UnitOfWork() as uow:
        assert uow.pipeline_jobs is not None
        existing = await uow.pipeline_jobs.get_latest(
            minute_token, job_type, owner_user_id=owner_user_id
        )
        if existing and existing.status in {STATUS_QUEUED, STATUS_RUNNING}:
            logger.info(
                "作业已在队列中，跳过重复入队 type=%s owner=%s token=%s status=%s",
                job_type,
                owner_user_id,
                minute_token,
                existing.status,
            )
            notify_worker()
            return existing.id
        entity = await uow.pipeline_jobs.create(
            PipelineJobCreateEntity(
                owner_user_id=owner_user_id,
                minute_token=minute_token,
                job_type=job_type,
                mode=encoded_mode,
                status=STATUS_QUEUED,
                stage="QUEUED",
                percent=0.0,
                attempt=0,
                max_attempts=max_attempts,
                started_at=None,
                broker_updated_at=now,
            )
        )
        await uow.commit()
        job_id = entity.id
    if job_type == JOB_SUMMARY:
        await set_summary_status(
            minute_token, "QUEUED", owner_user_id=owner_user_id
        )
    logger.info(
        "作业已入队 type=%s owner=%s token=%s id=%s",
        job_type,
        owner_user_id,
        minute_token,
        job_id,
    )
    notify_worker()
    return job_id


async def claim_next_job() -> PipelineJobEntity | None:
    async with UnitOfWork() as uow:
        assert uow.pipeline_jobs is not None
        queued = await uow.pipeline_jobs.list_by_statuses(
            [STATUS_QUEUED],
            job_types=WORKER_JOB_TYPES,
        )
        running = await uow.pipeline_jobs.list_by_statuses(
            [STATUS_RUNNING],
            job_types=WORKER_JOB_TYPES,
        )
        candidate = pick_claimable(queued, running)
        if candidate is None or candidate.id is None:
            return None
        claimed = await uow.pipeline_jobs.claim(candidate.id)
        if claimed is None:
            await uow.rollback()
            return None
        await uow.commit()
    if claimed.job_type == JOB_SUMMARY:
        await set_summary_status(
            claimed.minute_token,
            "GENERATING",
            owner_user_id=claimed.owner_user_id,
        )
    return claimed


async def requeue_orphaned_running(*, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> int:
    """进程重启后：进行中的作业无人收尾，退回队列或标失败。"""
    now = _now_ms()
    requeued = 0
    failed = 0
    summary_updates: list[tuple[str, int, str]] = []
    async with UnitOfWork() as uow:
        assert uow.pipeline_jobs is not None
        running = await uow.pipeline_jobs.list_by_statuses(
            [STATUS_RUNNING],
            job_types=WORKER_JOB_TYPES,
        )
        for job in running:
            if job.id is None:
                continue
            attempt = int(job.attempt or 0) + 1
            cap = int(job.max_attempts or max_attempts)
            if attempt >= cap:
                await uow.pipeline_jobs.update(
                    PipelineJobUpdateEntity(
                        id=job.id,
                        status=STATUS_FAILED,
                        stage="STALE_RECOVERED",
                        attempt=attempt,
                        error_message="进程重启时作业仍在运行，已超过最大重试次数",
                        finished_at=now,
                        broker_updated_at=now,
                    )
                )
                if job.job_type == JOB_SUMMARY:
                    summary_updates.append(
                        (job.minute_token, job.owner_user_id, "FAILED")
                    )
                failed += 1
                continue
            await uow.pipeline_jobs.update(
                PipelineJobUpdateEntity(
                    id=job.id,
                    status=STATUS_QUEUED,
                    stage="REQUEUED",
                    attempt=attempt,
                    error_message=None,
                    broker_updated_at=now,
                )
            )
            if job.job_type == JOB_SUMMARY:
                summary_updates.append(
                    (job.minute_token, job.owner_user_id, "QUEUED")
                )
            requeued += 1
        if requeued or failed:
            await uow.commit()
    for token, owner, status in summary_updates:
        await set_summary_status(token, status, owner_user_id=owner)
    if requeued or failed:
        logger.warning("启动回收作业：重入队 %d，放弃 %d", requeued, failed)
    notify_worker()
    return requeued + failed


async def fail_stale_running(
    *,
    summary_timeout_ms: int,
    share_video_timeout_ms: int,
    transcribe_timeout_ms: int = TRANSCRIBE_STALE_MS,
) -> int:
    now = _now_ms()
    marked = 0
    failed_summaries: list[tuple[str, int]] = []
    async with UnitOfWork() as uow:
        assert uow.pipeline_jobs is not None
        running = await uow.pipeline_jobs.list_by_statuses(
            [STATUS_RUNNING],
            job_types=WORKER_JOB_TYPES,
        )
        for job in running:
            if job.id is None:
                continue
            heartbeat = job.broker_updated_at or job.started_at or 0
            limit = {
                JOB_SHARE_VIDEO: share_video_timeout_ms,
                JOB_TRANSCRIBE: transcribe_timeout_ms,
            }.get(job.job_type, summary_timeout_ms)
            if now - int(heartbeat) < limit:
                continue
            await uow.pipeline_jobs.update(
                PipelineJobUpdateEntity(
                    id=job.id,
                    status=STATUS_FAILED,
                    stage="TIMEOUT",
                    error_message=f"作业心跳超时（>{limit // 60000} 分钟）已标记失败，可重新提交",
                    finished_at=now,
                    broker_updated_at=now,
                )
            )
            if job.job_type == JOB_SUMMARY:
                failed_summaries.append((job.minute_token, job.owner_user_id))
            marked += 1
        if marked:
            await uow.commit()
    for token, owner in failed_summaries:
        await set_summary_status(token, "FAILED", owner_user_id=owner)
    if marked:
        logger.warning("巡检：%d 条超时 RUNNING 作业已标失败", marked)
    return marked


def job_to_progress(
    job: PipelineJobEntity,
    *,
    extra_stage: str | None = None,
) -> dict:
    from app.service.llm_call_pool import llm_call_pool
    from app.service.summary_event_broker import SummaryStatus

    if job.status == STATUS_QUEUED:
        status = SummaryStatus.QUEUED.value
        stage = extra_stage or "排队中"
        percent = int(job.percent or 0)
    elif job.status == STATUS_RUNNING:
        status = SummaryStatus.GENERATING.value
        stage = extra_stage or job.stage or "处理中"
        percent = int(job.percent or 10)
    elif job.status == STATUS_FAILED:
        status = SummaryStatus.FAILED.value
        stage = job.stage or "失败"
        percent = int(job.percent or 0)
    else:
        status = SummaryStatus.COMPLETED.value
        stage = job.stage or "完成"
        percent = 100
    payload = {
        "minute_token": job.minute_token,
        "owner_user_id": job.owner_user_id,
        "status": status,
        "stage": stage,
        "percent": percent,
        "attempt": int(job.attempt or 0),
        "max_attempts": int(job.max_attempts or 0),
        "queue_position": 0,
        "error_message": job.error_message,
        "updated_at": None,
        "job_type": job.job_type,
    }
    payload.update(llm_call_pool.stats())
    return payload


async def resolve_progress(
    minute_token: str, *, owner_user_id: int
) -> dict | None:
    from app.service.summary_event_broker import summary_broker

    channel = summary_broker.get(minute_token, owner_user_id=owner_user_id)
    jobs = await latest_jobs(minute_token, owner_user_id=owner_user_id)
    summary_job = jobs.get(JOB_SUMMARY)
    video_job = jobs.get(JOB_SHARE_VIDEO)
    transcribe_job = jobs.get(JOB_TRANSCRIBE)
    video_running = bool(
        video_job and video_job.status in {STATUS_QUEUED, STATUS_RUNNING}
    )
    video_stage = None
    if video_job and video_running:
        if video_job.status == STATUS_QUEUED:
            video_stage = "分享片压缩排队中"
        else:
            video_stage = video_job.stage or "压缩分享片中"

    # 转写在纪要之前跑，进行中时它才是用户真正在等的那一步
    if transcribe_job and transcribe_job.status in {STATUS_QUEUED, STATUS_RUNNING}:
        stage = (
            "自建转写排队中"
            if transcribe_job.status == STATUS_QUEUED
            else f"自建转写 · {transcribe_job.stage or '处理中'}"
        )
        return job_to_progress(transcribe_job, extra_stage=stage)

    if channel is not None:
        snap = channel.snapshot()
        if video_stage:
            snap["stage"] = f"{snap.get('stage') or '生成中'} · {video_stage}"
        return snap
    if summary_job and summary_job.status in {
        STATUS_QUEUED,
        STATUS_RUNNING,
        STATUS_FAILED,
    }:
        extra = video_stage
        if summary_job.status == STATUS_QUEUED and video_stage:
            extra = f"纪要排队 · {video_stage}"
        return job_to_progress(summary_job, extra_stage=extra)
    if video_job and video_running:
        return job_to_progress(video_job, extra_stage=video_stage)
    # 转写失败时不会再入队纪要，这里兜住否则详情页只看到「暂无进度」
    if transcribe_job and transcribe_job.status == STATUS_FAILED:
        return job_to_progress(
            transcribe_job,
            extra_stage=f"自建转写失败 · {transcribe_job.stage or '未知阶段'}",
        )
    return None


async def latest_jobs(
    minute_token: str, *, owner_user_id: int
) -> dict[str, PipelineJobEntity | None]:
    async with UnitOfWork() as uow:
        assert uow.pipeline_jobs is not None
        summary = await uow.pipeline_jobs.get_latest(
            minute_token, JOB_SUMMARY, owner_user_id=owner_user_id
        )
        video = await uow.pipeline_jobs.get_latest(
            minute_token, JOB_SHARE_VIDEO, owner_user_id=owner_user_id
        )
        transcribe = await uow.pipeline_jobs.get_latest(
            minute_token, JOB_TRANSCRIBE, owner_user_id=owner_user_id
        )
    return {
        JOB_SUMMARY: summary,
        JOB_SHARE_VIDEO: video,
        JOB_TRANSCRIBE: transcribe,
    }
