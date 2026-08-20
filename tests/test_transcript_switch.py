"""手动切换转写来源。

这是个破坏性操作：切一次要删文件、作废说话人编号、重跑纪要。所以每一条拒绝的
理由都得在动手之前给出来，不能走到一半才发现干不了。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import runtime_config
from app.core.db_base import Base
from app.data_model.entity.meeting_record import MeetingRecordCreateEntity
from app.data_model.entity.pipeline_job import PipelineJobCreateEntity
from app.repository.uow import UnitOfWork
from app.service import transcript_switch_service as switcher
from app.service.meeting_storage_service import (
    ASR_TRANSCRIPT_FILENAME,
    MeetingStorageService,
)
from app.service.pipeline_queue import JOB_TRANSCRIBE, STATUS_QUEUED
from app.service.transcription_flow import SOURCE_ASR, SOURCE_FEISHU

OWNER = 7
TOKEN = "obswitch00001"


@pytest.fixture
def _memory_db(monkeypatch: pytest.MonkeyPatch):
    """把 UnitOfWork 指到内存库，避免测试写进开发机的 sqlite。"""
    from app.repository.orm import meeting_record, pipeline_job, voiceprint  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async def create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create())
    monkeypatch.setattr(
        "app.repository.uow.async_session_factory",
        async_sessionmaker(engine, expire_on_commit=False),
    )
    yield
    asyncio.run(engine.dispose())


class FakeR2:
    @staticmethod
    def enabled() -> bool:
        return True


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """本地有飞书原文与自建转写各一份，外加拦下所有入队。"""
    storage = MeetingStorageService(storage_root=str(tmp_path))
    layout = storage.ensure_layout(TOKEN, owner_user_id=OWNER)
    (layout["transcript"] / "transcript.txt").write_text(
        "张三 00:00:00.000 \n飞书那份\n", encoding="utf-8"
    )
    asr_file = layout["transcript"] / ASR_TRANSCRIPT_FILENAME
    asr_file.write_text("说话人1 00:00:00.000 \n自建那份\n", encoding="utf-8")
    (layout["media"] / "recording.m4a").write_bytes(b"audio")

    enqueued: list[str] = []

    async def fake_enqueue_job(minute_token, *, owner_user_id, job_type, **kwargs):
        enqueued.append(f"{job_type}:{kwargs.get('mode')}")
        return 42

    async def fake_enqueue_transcribe(minute_token, *, owner_user_id):
        enqueued.append(JOB_TRANSCRIBE)
        return 43

    async def fake_can_self_transcribe(minute_token, *, owner_user_id):
        return True

    async def fake_refresh(minute_token, *, owner_user_id):
        return []

    monkeypatch.setattr(switcher, "_storage", storage)
    monkeypatch.setattr(switcher, "enqueue_job", fake_enqueue_job)
    monkeypatch.setattr(switcher, "enqueue_transcribe", fake_enqueue_transcribe)
    monkeypatch.setattr(switcher, "can_self_transcribe", fake_can_self_transcribe)
    monkeypatch.setattr(switcher, "refresh_speakers_json", fake_refresh)
    monkeypatch.setattr("app.service.r2_media_service.r2_media_service", FakeR2())
    runtime_config.replace_cache({"STEP_ASR_ENABLED": "true"})
    yield storage, asr_file, enqueued
    runtime_config.replace_cache({})


async def _seed_record(*, transcript_source: str | None, event_type: str = "MANUAL"):
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        record = await uow.meeting_records.create(
            MeetingRecordCreateEntity(
                feishu_event_id=f"seed-{TOKEN}",
                event_type=event_type,
                minute_token=TOKEN,
                status="COMPLETED",
                storage_path="whatever",
                owner_user_id=OWNER,
                transcript_source=transcript_source,
            )
        )
        await uow.commit()
        return record


async def _seed_running_transcribe():
    async with UnitOfWork() as uow:
        assert uow.pipeline_jobs is not None
        await uow.pipeline_jobs.create(
            PipelineJobCreateEntity(
                owner_user_id=OWNER,
                minute_token=TOKEN,
                job_type=JOB_TRANSCRIBE,
                status=STATUS_QUEUED,
            )
        )
        await uow.commit()


async def _load_source():
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        record = await uow.meeting_records.get_latest_by_minute_token(
            TOKEN, owner_user_id=OWNER
        )
        return record.transcript_source


def test_source_falls_back_to_whichever_file_is_on_disk(rig):
    """DB 字段可能是空的（旧数据），落盘的文件才是读路径的真相。"""
    storage, asr_file, _ = rig

    assert (
        switcher.resolve_current_source(None, TOKEN, owner_user_id=OWNER)
        == SOURCE_ASR
    )

    asr_file.unlink()

    assert (
        switcher.resolve_current_source(None, TOKEN, owner_user_id=OWNER)
        == SOURCE_FEISHU
    )


@pytest.mark.usefixtures("_memory_db")
def test_switching_to_asr_clears_the_source_until_it_actually_runs(rig):
    """先写 asr 的话，转写失败之后覆盖率判定就再也重来不了了。"""
    _, _, enqueued = rig

    async def scenario():
        await _seed_record(transcript_source=SOURCE_FEISHU)
        result = await switcher.switch_to_asr(TOKEN, owner_user_id=OWNER)
        return result, await _load_source()

    result, source = asyncio.run(scenario())

    assert result.job_id == 43
    assert enqueued == [JOB_TRANSCRIBE]
    assert source is None


@pytest.mark.usefixtures("_memory_db")
def test_switching_to_asr_needs_the_engine_switched_on(rig):
    runtime_config.replace_cache({"STEP_ASR_ENABLED": "false"})

    with pytest.raises(switcher.TranscriptSwitchError, match="自建转写已被关闭"):
        asyncio.run(switcher.switch_to_asr(TOKEN, owner_user_id=OWNER))


@pytest.mark.usefixtures("_memory_db")
def test_switching_to_asr_needs_r2(rig, monkeypatch: pytest.MonkeyPatch):
    class Disabled(FakeR2):
        @staticmethod
        def enabled() -> bool:
            return False

    monkeypatch.setattr("app.service.r2_media_service.r2_media_service", Disabled())

    with pytest.raises(switcher.TranscriptSwitchError, match="R2"):
        asyncio.run(switcher.switch_to_asr(TOKEN, owner_user_id=OWNER))


@pytest.mark.usefixtures("_memory_db")
def test_a_transcribe_already_in_flight_is_reported_not_swallowed(rig):
    """入队对同类型进行中作业是静默去重的，手动切换必须自己把话说出来。"""

    async def scenario():
        await _seed_record(transcript_source=SOURCE_FEISHU)
        await _seed_running_transcribe()
        await switcher.switch_to_asr(TOKEN, owner_user_id=OWNER)

    with pytest.raises(switcher.TranscriptSwitchError, match="已经有自建转写"):
        asyncio.run(scenario())


@pytest.mark.usefixtures("_memory_db")
def test_switching_back_to_feishu_removes_the_asr_transcript(rig):
    """只要自建那份还在，所有读路径都优先读它。"""
    _, asr_file, enqueued = rig

    async def scenario():
        await _seed_record(transcript_source=SOURCE_ASR)
        result = await switcher.switch_to_feishu(TOKEN, owner_user_id=OWNER)
        return result, await _load_source()

    result, source = asyncio.run(scenario())

    assert asr_file.exists() is False
    assert source == SOURCE_FEISHU
    # 纪要必须强制重跑，否则页面上还挂着按自建转写写出来的那份
    assert enqueued == ["SUMMARY:FULL|force"]
    assert result.source == SOURCE_FEISHU


@pytest.mark.usefixtures("_memory_db")
def test_switching_back_to_feishu_drops_the_asr_speaker_numbering(rig):
    """「说话人N」那套编号连同样本一起作废，留着只会让展示层贴错名字。"""
    from app.data_model.entity.voiceprint import (
        MeetingSpeakerUpsertEntity,
        VoiceprintCreateEntity,
        VoiceprintSampleCreateEntity,
    )
    from app.service import voiceprint_service as vp

    async def scenario():
        await _seed_record(transcript_source=SOURCE_ASR)
        async with UnitOfWork() as uow:
            assert uow.voiceprints is not None
            person = await uow.voiceprints.create(
                VoiceprintCreateEntity(
                    embedding=vp.encode_embedding([1.0, 0.0]),
                    dim=2,
                    sample_count=1,
                    meeting_count=1,
                )
            )
            await uow.voiceprints.upsert_speaker(
                MeetingSpeakerUpsertEntity(
                    owner_user_id=OWNER,
                    minute_token=TOKEN,
                    local_label="说话人1",
                    voiceprint_id=person.id,
                    match_score=0.9,
                    talk_ms=8000,
                    segments=1,
                )
            )
            await uow.voiceprints.add_sample(
                VoiceprintSampleCreateEntity(
                    voiceprint_id=person.id,
                    owner_user_id=OWNER,
                    minute_token=TOKEN,
                    start_ms=0,
                    end_ms=8000,
                    score=0.9,
                )
            )
            await uow.commit()

        await switcher.switch_to_feishu(TOKEN, owner_user_id=OWNER)

        async with UnitOfWork() as uow:
            assert uow.voiceprints is not None
            speakers = await uow.voiceprints.list_speakers(
                TOKEN, owner_user_id=OWNER
            )
            samples = await uow.voiceprints.list_samples(person.id)
        return speakers, samples

    speakers, samples = asyncio.run(scenario())

    assert speakers == []
    assert samples == []
