"""更新实体的「没提」与「置空」。

这两件事以前都是 None，于是置空写不进去，还不报错——转写来源就是这么丢的。现在
默认值是 UNSET，显式的 None 才是置空。这里两个方向都钉住：该改的改了，没提的没被
顺手抹掉。
"""

from __future__ import annotations

import asyncio

import pytest

from app.data_model.entity.meeting_record import (
    MeetingRecordCreateEntity,
    MeetingRecordUpdateEntity,
)
from app.data_model.entity.pipeline_job import (
    PipelineJobCreateEntity,
    PipelineJobUpdateEntity,
)
from app.repository.uow import UnitOfWork

OWNER = 7
TOKEN = "obunset000001"


async def _seed_record(**kwargs) -> int:
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        record = await uow.meeting_records.create(
            MeetingRecordCreateEntity(
                feishu_event_id=f"evt:{TOKEN}",
                event_type="MANUAL_DOWNLOAD",
                minute_token=TOKEN,
                owner_user_id=OWNER,
                status="COMPLETED",
                **kwargs,
            )
        )
        await uow.commit()
        return record.id


async def _load_record():
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        return await uow.meeting_records.get_latest_by_minute_token(
            TOKEN, owner_user_id=OWNER
        )


@pytest.mark.usefixtures("_memory_db")
def test_an_explicit_none_clears_the_field():
    async def scenario():
        record_id = await _seed_record(transcript_source="feishu")
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            await uow.meeting_records.update(
                MeetingRecordUpdateEntity(id=record_id, transcript_source=None)
            )
            await uow.commit()
        return await _load_record()

    assert asyncio.run(scenario()).transcript_source is None


@pytest.mark.usefixtures("_memory_db")
def test_fields_left_out_keep_their_value():
    """更新实体把整张表的字段都摆着，没提的绝不能跟着被抹掉。"""

    async def scenario():
        record_id = await _seed_record(transcript_source="feishu", title="旧标题")
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            await uow.meeting_records.update(
                MeetingRecordUpdateEntity(id=record_id, title="新标题")
            )
            await uow.commit()
        return await _load_record()

    record = asyncio.run(scenario())

    assert record.title == "新标题"
    assert record.transcript_source == "feishu"


@pytest.mark.usefixtures("_memory_db")
def test_merging_a_create_entity_does_not_wipe_what_it_did_not_carry():
    """按 token 合并主档时，建档实体里没填的字段一律是 None，不是置空的意思。"""

    async def scenario():
        await _seed_record(title="旧标题", duration_ms=1234)
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            await uow.meeting_records.upsert_by_minute_token(
                MeetingRecordCreateEntity(
                    feishu_event_id=f"evt:{TOKEN}",
                    event_type="MANUAL_DOWNLOAD",
                    minute_token=TOKEN,
                    owner_user_id=OWNER,
                    title="新标题",
                )
            )
            await uow.commit()
        return await _load_record()

    record = asyncio.run(scenario())

    assert record.title == "新标题"
    assert record.duration_ms == 1234


@pytest.mark.usefixtures("_memory_db")
def test_a_heartbeat_only_update_keeps_the_job_intact():
    """心跳是最常见的一次更新，它不该顺手把阶段和进度清空。"""
    from app.service.pipeline_job_service import update_job

    async def scenario():
        async with UnitOfWork() as uow:
            assert uow.pipeline_jobs is not None
            job = await uow.pipeline_jobs.create(
                PipelineJobCreateEntity(
                    owner_user_id=OWNER,
                    minute_token=TOKEN,
                    job_type="SUMMARY",
                    status="RUNNING",
                    stage="成文",
                    percent=42.0,
                )
            )
            await uow.commit()
            job_id = job.id
        await update_job(job_id)
        async with UnitOfWork() as uow:
            assert uow.pipeline_jobs is not None
            return await uow.pipeline_jobs.get_latest(
                TOKEN, "SUMMARY", owner_user_id=OWNER
            )

    job = asyncio.run(scenario())

    assert (job.status, job.stage, job.percent) == ("RUNNING", "成文", 42.0)


@pytest.mark.usefixtures("_memory_db")
def test_requeueing_a_job_drops_the_stale_error():
    """重入队之后还挂着上一轮的报错，看着像是又失败了一次。"""

    async def scenario():
        async with UnitOfWork() as uow:
            assert uow.pipeline_jobs is not None
            job = await uow.pipeline_jobs.create(
                PipelineJobCreateEntity(
                    owner_user_id=OWNER,
                    minute_token=TOKEN,
                    job_type="SUMMARY",
                    status="FAILED",
                    error_message="上一轮的报错",
                )
            )
            await uow.pipeline_jobs.update(
                PipelineJobUpdateEntity(
                    id=job.id, status="QUEUED", error_message=None
                )
            )
            await uow.commit()
            return await uow.pipeline_jobs.get_latest(
                TOKEN, "SUMMARY", owner_user_id=OWNER
            )

    job = asyncio.run(scenario())

    assert job.status == "QUEUED"
    assert job.error_message is None
