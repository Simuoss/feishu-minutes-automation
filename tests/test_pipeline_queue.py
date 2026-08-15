"""作业队列认领规则与进度合成。"""

from app.data_model.entity.pipeline_job import PipelineJobEntity
from app.integrations.video.ffmpeg_client import parse_ffmpeg_out_time_ms
from app.service.pipeline_queue import (
    JOB_SHARE_VIDEO,
    JOB_SUMMARY,
    JOB_TRANSCRIBE,
    STATUS_QUEUED,
    STATUS_RUNNING,
    encode_job_mode,
    job_to_progress,
    parse_job_mode,
    pick_claimable,
)


def _job(
    *,
    job_id: int,
    job_type: str,
    status: str,
    owner: int = 1,
) -> PipelineJobEntity:
    return PipelineJobEntity(
        id=job_id,
        owner_user_id=owner,
        minute_token=f"tok{job_id}",
        job_type=job_type,
        mode="FULL",
        status=status,
        stage=None,
        percent=0,
        attempt=0,
        max_attempts=3,
        error_message=None,
        started_at=None,
        finished_at=None,
        broker_updated_at=None,
    )


def test_parse_and_encode_job_mode():
    assert parse_job_mode("FULL|force") == ("FULL", True)
    assert parse_job_mode("WRITE") == ("WRITE", False)
    assert encode_job_mode("full", force=True) == "FULL|force"


def test_pick_claimable_skips_busy_owner_summary():
    queued = [
        _job(job_id=2, job_type=JOB_SUMMARY, status=STATUS_QUEUED, owner=1),
        _job(job_id=3, job_type=JOB_SUMMARY, status=STATUS_QUEUED, owner=4),
    ]
    running = [_job(job_id=1, job_type=JOB_SUMMARY, status=STATUS_RUNNING, owner=1)]
    picked = pick_claimable(queued, running)
    assert picked is not None
    assert picked.id == 3


def test_pick_claimable_limits_share_video():
    queued = [_job(job_id=2, job_type=JOB_SHARE_VIDEO, status=STATUS_QUEUED)]
    running = [_job(job_id=1, job_type=JOB_SHARE_VIDEO, status=STATUS_RUNNING)]
    assert pick_claimable(queued, running) is None
    assert pick_claimable(queued, running, max_share_video=2) is not None


def test_pick_claimable_limits_transcribe():
    """转写要抽音轨、跑 onnx，同时只许跑一个，但不能挡住别的类型。"""
    queued = [
        _job(job_id=2, job_type=JOB_TRANSCRIBE, status=STATUS_QUEUED, owner=2),
        _job(job_id=3, job_type=JOB_SUMMARY, status=STATUS_QUEUED, owner=3),
    ]
    running = [_job(job_id=1, job_type=JOB_TRANSCRIBE, status=STATUS_RUNNING)]
    picked = pick_claimable(queued, running)
    assert picked is not None
    assert picked.id == 3
    assert pick_claimable(queued[:1], running, max_transcribe=2) is not None


def test_job_to_progress_carries_job_type_for_frontend():
    """前端靠 job_type 区分该轮询转写还是接 SSE。"""
    job = _job(job_id=1, job_type=JOB_TRANSCRIBE, status=STATUS_RUNNING)
    job.stage = "云端识别 2/4 段"
    payload = job_to_progress(job, extra_stage="自建转写 · 云端识别 2/4 段")
    assert payload["job_type"] == JOB_TRANSCRIBE
    assert payload["stage"] == "自建转写 · 云端识别 2/4 段"
    assert payload["status"] == "GENERATING"


def test_parse_ffmpeg_out_time_ms():
    assert parse_ffmpeg_out_time_ms("out_time_ms=1500000") == 1_500_000
    assert parse_ffmpeg_out_time_ms("progress=continue") is None


def test_owner_summary_gate_reports_wait():
    import asyncio

    from app.service.owner_summary_gate import OwnerSummaryGate

    gate = OwnerSummaryGate()
    stages: list[str] = []
    first_inside = asyncio.Event()
    release_first = asyncio.Event()

    async def first():
        async with gate.hold(7):
            first_inside.set()
            await release_first.wait()

    async def second():
        await first_inside.wait()
        async with gate.hold(7, on_wait=stages.append):
            pass

    async def scenario():
        t1 = asyncio.create_task(first())
        t2 = asyncio.create_task(second())
        await first_inside.wait()
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(t1, t2)

    asyncio.run(scenario())
    assert stages == ["同账号上一场纪要进行中，排队等待"]
