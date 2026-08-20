"""从飞书转写的真名自动提炼声纹。

飞书给的姓名可信、切分不可信，所以这里盯的是两件事：区间怎么从只有段首时间的
原文里还原出来，以及比对结果落成提案而不是直接改人物库。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import runtime_config
from app.core.db_base import Base
from app.data_model.entity.voiceprint import (
    PROPOSAL_APPROVED,
    PROPOSAL_PENDING,
    PROPOSAL_REJECTED,
    VoiceprintCreateEntity,
)
from app.repository.uow import UnitOfWork
from app.service import voiceprint_harvest_service as harvest
from app.service import voiceprint_service as vp

OWNER = 7
TOKEN = "obharvest0001"


@pytest.fixture
def _memory_db(monkeypatch: pytest.MonkeyPatch):
    """把 UnitOfWork 指到内存库，避免测试写进开发机的 sqlite。"""
    from app.repository.orm import meeting_record, voiceprint  # noqa: F401

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


@pytest.fixture
def _thresholds():
    runtime_config.replace_cache({"SPEAKER_MATCH_THRESHOLD": "0.55"})
    yield
    runtime_config.replace_cache({})


def _transcript(*heads: tuple[str, str]) -> str:
    lines = ["00:10:00", ""]
    for name, clock in heads:
        lines.append(f"{name} {clock} ")
        lines.append("随便说了点什么")
        lines.append("")
    return "\n".join(lines)


@pytest.mark.parametrize(
    "name",
    ["说话人1", "发言人 2", "讲话人3", "Speaker 4", "spk5", "  ", ""],
)
def test_placeholder_names_are_not_real_names(name: str):
    assert harvest.is_placeholder_name(name) is True


@pytest.mark.parametrize("name", ["张三", "李 四", "Alice", "Speaker Wang"])
def test_real_names_survive_the_filter(name: str):
    assert harvest.is_placeholder_name(name) is False


def test_span_ends_at_the_next_segment_start():
    """段头只有起点，结束时间只能借下一段的起点。"""
    transcript = _transcript(
        ("张三", "00:00:10.000"),
        ("李四", "00:01:00.000"),
        ("张三", "00:02:00.000"),
    )

    spans = harvest.parse_named_spans(transcript, duration_ms=200_000)

    assert spans["张三"] == [(10.0, 60.0), (120.0, 200.0)]
    assert spans["李四"] == [(60.0, 120.0)]


def test_tail_span_falls_back_to_a_fixed_length_without_duration():
    """末段没有下一段，也没有会议时长可借，就按固定时长估。"""
    transcript = _transcript(("张三", "00:00:10.000"))

    spans = harvest.parse_named_spans(transcript)

    assert spans["张三"] == [(10.0, 10.0 + harvest.TAIL_SEGMENT_SECONDS)]


def test_placeholder_segments_are_dropped():
    transcript = _transcript(
        ("说话人1", "00:00:00.000"),
        ("张三", "00:00:30.000"),
    )

    spans = harvest.parse_named_spans(transcript, duration_ms=90_000)

    assert list(spans) == ["张三"]


def _speaker(name: str, vector: list[float]) -> harvest._NamedSpeaker:
    samples = [
        vp.SpeakerSample(start_ms=0, end_ms=8000, embedding=vector, rms=0.1)
    ]
    return harvest._NamedSpeaker(
        name=name, spans=[(0.0, 8.0)], samples=samples, centroid=vector
    )


async def _add_person(embedding: list[float], display_name: str | None):
    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        person = await uow.voiceprints.create(
            VoiceprintCreateEntity(
                embedding=vp.encode_embedding(embedding),
                dim=len(embedding),
                sample_count=3,
                meeting_count=1,
                display_name=display_name,
            )
        )
        await uow.commit()
        return person


async def _pending():
    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        return await uow.voiceprints.list_proposals(status=PROPOSAL_PENDING)


@pytest.mark.usefixtures("_memory_db", "_thresholds")
def test_unknown_voice_proposes_a_brand_new_person():
    async def scenario():
        proposals, skipped = await harvest._propose(
            [_speaker("张三", [1.0, 0.0])],
            minute_token=TOKEN,
            owner_user_id=OWNER,
        )
        return proposals, skipped, await _pending()

    proposals, skipped, pending = asyncio.run(scenario())

    assert (proposals, skipped) == (1, 0)
    assert pending[0].proposed_name == "张三"
    # 空的 voiceprint_id 就是「库里没有这个人，建议新建」
    assert pending[0].voiceprint_id is None
    assert pending[0].score is None


@pytest.mark.usefixtures("_memory_db", "_thresholds")
def test_matched_but_nameless_person_gets_a_naming_proposal():
    async def scenario():
        person = await _add_person([1.0, 0.0], None)
        proposals, _ = await harvest._propose(
            [_speaker("张三", [1.0, 0.0])],
            minute_token=TOKEN,
            owner_user_id=OWNER,
        )
        return person.id, proposals, await _pending()

    person_id, proposals, pending = asyncio.run(scenario())

    assert proposals == 1
    assert pending[0].voiceprint_id == person_id
    assert pending[0].score == pytest.approx(1.0)


@pytest.mark.usefixtures("_memory_db", "_thresholds")
def test_already_known_same_name_only_adds_samples():
    """认识而且名字就是这个，没什么要人确认的，补样本就好。"""

    async def scenario():
        person = await _add_person([1.0, 0.0], "张三")
        proposals, skipped = await harvest._propose(
            [_speaker("张三", [1.0, 0.0])],
            minute_token=TOKEN,
            owner_user_id=OWNER,
        )
        async with UnitOfWork() as uow:
            assert uow.voiceprints is not None
            samples = await uow.voiceprints.list_samples(person.id)
        return proposals, skipped, samples, await _pending()

    proposals, skipped, samples, pending = asyncio.run(scenario())

    assert (proposals, skipped) == (0, 1)
    assert pending == []
    assert len(samples) == 1


@pytest.mark.usefixtures("_memory_db", "_thresholds")
def test_rerun_replaces_the_previous_pending_batch():
    """同一场会议重跑不该把待确认列表越堆越长。"""

    async def scenario():
        for _ in range(2):
            await harvest._propose(
                [_speaker("张三", [1.0, 0.0])],
                minute_token=TOKEN,
                owner_user_id=OWNER,
            )
        return await _pending()

    assert len(asyncio.run(scenario())) == 1


@pytest.mark.usefixtures("_memory_db", "_thresholds")
def test_approving_a_new_person_writes_the_name_and_samples(
    monkeypatch: pytest.MonkeyPatch,
):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "app.service.transcription_flow.refresh_speakers_json", noop
    )

    async def scenario():
        await harvest._propose(
            [_speaker("张三", [1.0, 0.0])],
            minute_token=TOKEN,
            owner_user_id=OWNER,
        )
        pending = await _pending()
        decided = await harvest.approve_proposal(pending[0].id)
        async with UnitOfWork() as uow:
            assert uow.voiceprints is not None
            people = await uow.voiceprints.list_active()
        return decided, people

    decided, people = asyncio.run(scenario())

    assert decided.status == PROPOSAL_APPROVED
    assert [p.display_name for p in people] == ["张三"]


@pytest.mark.usefixtures("_memory_db", "_thresholds")
def test_approving_an_existing_person_renames_instead_of_duplicating(
    monkeypatch: pytest.MonkeyPatch,
):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "app.service.transcription_flow.refresh_speakers_json", noop
    )

    async def scenario():
        await _add_person([1.0, 0.0], None)
        await harvest._propose(
            [_speaker("张三", [1.0, 0.0])],
            minute_token=TOKEN,
            owner_user_id=OWNER,
        )
        pending = await _pending()
        await harvest.approve_proposal(pending[0].id)
        async with UnitOfWork() as uow:
            assert uow.voiceprints is not None
            return await uow.voiceprints.list_active()

    people = asyncio.run(scenario())

    assert len(people) == 1
    assert people[0].display_name == "张三"


@pytest.mark.usefixtures("_memory_db", "_thresholds")
def test_rejected_proposal_leaves_the_library_alone():
    async def scenario():
        await harvest._propose(
            [_speaker("张三", [1.0, 0.0])],
            minute_token=TOKEN,
            owner_user_id=OWNER,
        )
        pending = await _pending()
        decided = await harvest.reject_proposal(pending[0].id)
        async with UnitOfWork() as uow:
            assert uow.voiceprints is not None
            people = await uow.voiceprints.list_active()
        return decided, people

    decided, people = asyncio.run(scenario())

    assert decided.status == PROPOSAL_REJECTED
    assert people == []


@pytest.mark.usefixtures("_memory_db", "_thresholds")
def test_a_decided_proposal_cannot_be_approved_twice():
    async def scenario():
        await harvest._propose(
            [_speaker("张三", [1.0, 0.0])],
            minute_token=TOKEN,
            owner_user_id=OWNER,
        )
        pending = await _pending()
        await harvest.reject_proposal(pending[0].id)
        await harvest.approve_proposal(pending[0].id)

    with pytest.raises(harvest.VoiceprintHarvestError, match="已经处理过"):
        asyncio.run(scenario())
