"""消费 QUEUED 作业：纪要走主 loop，压片丢到线程以免堵事件循环。"""

from __future__ import annotations

import asyncio
import logging

from app.core import runtime_config
from app.data_model.entity.pipeline_job import PipelineJobEntity
from app.service.pipeline_job_service import update_job
from app.service.pipeline_queue import (
    JOB_SHARE_VIDEO,
    JOB_SUMMARY,
    STATUS_COMPLETED,
    STATUS_FAILED,
    fail_stale_running,
    parse_job_mode,
    requeue_orphaned_running,
    wait_for_work,
    claim_next_job,
)
from app.service.r2_media_service import r2_media_service
from app.service.summary_event_broker import summary_broker
from app.service.summary_generation_service import summary_generation_service

logger = logging.getLogger(__name__)

HEAL_INTERVAL_SECONDS = 60.0
SUMMARY_STALE_MS = int(2 * 3600 * 1000)


class PipelineWorker:
    def __init__(self) -> None:
        self._loop_task: asyncio.Task[None] | None = None
        self._heal_task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()
        self._inflight_job_ids: set[int] = set()

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = loop.create_task(self._run(), name="pipeline-worker")
        if self._heal_task is None or self._heal_task.done():
            self._heal_task = loop.create_task(self._heal_loop(), name="pipeline-heal")
        logger.info("流水线 worker 已启动")

    async def stop(self) -> None:
        for task in (self._loop_task, self._heal_task, *list(self._inflight)):
            if task is not None:
                task.cancel()
        self._inflight.clear()
        self._inflight_job_ids.clear()

    async def _run(self) -> None:
        while True:
            try:
                job = await claim_next_job()
                if job is not None:
                    task = asyncio.create_task(
                        self._execute(job),
                        name=f"job-{job.job_type}-{job.id}",
                    )
                    self._inflight.add(task)
                    task.add_done_callback(self._inflight.discard)
                    continue
                if self._inflight:
                    await asyncio.wait(
                        self._inflight,
                        timeout=1.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    await wait_for_work(timeout=2.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("流水线 worker 循环异常，2s 后继续")
                await asyncio.sleep(2.0)

    async def _heartbeat_inflight(self) -> None:
        for job_id in list(self._inflight_job_ids):
            await update_job(job_id)

    async def _heal_loop(self) -> None:
        await requeue_orphaned_running()
        while True:
            try:
                await self._heartbeat_inflight()
                compress_s = runtime_config.get_float(
                    "R2_VIDEO_COMPRESS_TIMEOUT_SECONDS", 21600.0
                )
                await fail_stale_running(
                    summary_timeout_ms=SUMMARY_STALE_MS,
                    share_video_timeout_ms=int((compress_s + 600) * 1000),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("作业巡检失败，将在下一轮重试")
            await asyncio.sleep(HEAL_INTERVAL_SECONDS)

    async def _execute(self, job: PipelineJobEntity) -> None:
        assert job.id is not None
        self._inflight_job_ids.add(job.id)
        try:
            if job.job_type == JOB_SUMMARY:
                await self._run_summary(job)
            elif job.job_type == JOB_SHARE_VIDEO:
                await self._run_share_video(job)
            else:
                await update_job(
                    job.id,
                    status=STATUS_FAILED,
                    stage="UNKNOWN_TYPE",
                    error_message=f"未知作业类型 {job.job_type}",
                    finished=True,
                )
        except Exception as exc:
            logger.exception(
                "作业执行失败 type=%s id=%s token=%s",
                job.job_type,
                job.id,
                job.minute_token,
            )
            await update_job(
                job.id,
                status=STATUS_FAILED,
                stage="FAILED",
                error_message=str(exc),
                finished=True,
            )
        finally:
            self._inflight_job_ids.discard(job.id)

    async def _run_summary(self, job: PipelineJobEntity) -> None:
        mode, force = parse_job_mode(job.mode)
        broker = summary_broker.bind(
            job.minute_token, owner_user_id=job.owner_user_id
        )
        broker.queue(position=0)
        result = await summary_generation_service.generate(
            job.minute_token,
            owner_user_id=job.owner_user_id,
            force=force,
            mode=mode,
            pipeline_job_id=job.id,
        )
        if result.status == "FAILED":
            await update_job(
                job.id,
                status=STATUS_FAILED,
                stage="FAILED",
                error_message=result.error_message,
                finished=True,
            )
            return
        if result.status != "COMPLETED":
            return
        await update_job(
            job.id,
            status=STATUS_COMPLETED,
            stage="DONE",
            percent=100.0,
            finished=True,
        )

    async def _run_share_video(self, job: PipelineJobEntity) -> None:
        loop = asyncio.get_running_loop()

        def on_progress(percent: float) -> None:
            mapped = min(90.0, 10.0 + max(0.0, percent) * 0.8)
            stage = f"压缩分享片 {percent:.0f}%"
            asyncio.run_coroutine_threadsafe(
                update_job(job.id, stage=stage, percent=mapped),
                loop,
            )

        await update_job(job.id, stage="COMPRESS", percent=5.0)
        ok = await r2_media_service.ensure_share_video(
            job.minute_token,
            owner_user_id=job.owner_user_id,
            on_progress=on_progress,
        )
        if ok:
            await update_job(
                job.id,
                status=STATUS_COMPLETED,
                stage="DONE",
                percent=100.0,
                finished=True,
            )
            return
        await update_job(
            job.id,
            status=STATUS_FAILED,
            stage="FAILED",
            error_message="分享视频压缩或上传未成功",
            finished=True,
        )


pipeline_worker = PipelineWorker()
