"""自建转写链路：覆盖率判定、声纹认人、以及假 ASR 下的整条作业。

不碰真实的 ffmpeg、R2 与阶跃接口，数据库换成内存 SQLite，所以可以在 CI 里跑。
"""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import runtime_config
from app.core.db_base import Base
from app.data_model.entity.voiceprint import VoiceprintUpdateEntity
from app.integrations.asr.step_asr_client import (
    AsrRequestError,
    AsrResult,
    AsrUtterance,
)
from app.repository.uow import UnitOfWork
from app.service import speaker_naming_service
from app.service import voiceprint_service as vp
from app.service.asr_transcript import TranscriptLine, build_transcript
from app.service.meeting_storage_service import MeetingStorageService
from app.service.transcription_service import (
    TranscriptionError,
    TranscriptionService,
    _ChunkUtterance,
    needs_self_transcription,
    transcript_coverage,
)

OWNER = 7
TOKEN = "obasr000001"
TOKEN2 = "obasr000002"

# 两个人的声纹向量：正交，余弦相似度 0，绝不会被判成同一个人
VOICE_A = [1.0, 0.02, 0.0]
VOICE_B = [0.0, 0.03, 1.0]

CHUNK_SECONDS = 900.0
TOTAL_SECONDS = 1800.0


# ---------- 覆盖率判定 ----------


def test_transcript_coverage_uses_last_timestamp():
    transcript = build_transcript(
        [
            TranscriptLine(speaker="说话人1", start_ms=0, end_ms=1000, text="开场。"),
            TranscriptLine(
                speaker="说话人2", start_ms=180_000, end_ms=182_000, text="三分钟处。"
            ),
        ],
        duration_ms=3_600_000,
    )
    coverage = transcript_coverage(transcript, 3_600_000)
    assert coverage is not None
    assert abs(coverage - 0.05) < 0.01


def test_transcript_coverage_edge_cases():
    # 没时长就无从判断，交给调用方沿用飞书转写
    assert transcript_coverage("说话人1 00:00:01.000 \n在。", None) is None
    assert transcript_coverage(None, 60_000) == 0.0
    assert transcript_coverage("没有任何时间戳的文本", 60_000) == 0.0
    # 时间戳超过时长（转写带尾巴）时截到 1.0，不该出现 >1 的覆盖率
    assert transcript_coverage("说话人1 00:02:00.000 \n在。", 60_000) == 1.0


def test_needs_self_transcription_respects_switch_and_threshold(
    _runtime_defaults: None,
):
    assert needs_self_transcription(0.05) is True
    assert needs_self_transcription(0.95) is False
    # 判不出覆盖率时保守处理，不擅自烧一次云端识别
    assert needs_self_transcription(None) is False

    runtime_config.replace_cache({"STEP_ASR_ENABLED": "false"})
    assert needs_self_transcription(0.05) is False


# ---------- 声纹计算 ----------


def test_embedding_roundtrip_and_cosine():
    blob = vp.encode_embedding(VOICE_A)
    restored = vp.decode_embedding(blob)
    assert len(restored) == 3
    assert vp.cosine(restored, VOICE_A) > 0.999
    assert vp.cosine(VOICE_A, VOICE_B) < 0.1
    assert vp.cosine([], VOICE_A) == 0.0
    assert vp.cosine([1.0, 0.0], VOICE_A) == 0.0


def test_weighted_merge_leans_towards_heavier_side():
    merged = vp.weighted_merge([1.0, 0.0], 9, [0.0, 1.0], 1)
    assert merged[0] == pytest.approx(0.9)
    assert merged[1] == pytest.approx(0.1)
    assert vp.weighted_merge([], 1, VOICE_A, 1) == VOICE_A


def test_trimmed_centroid_drops_outlier():
    good = [[1.0, 0.0], [0.98, 0.05], [0.99, 0.02], [1.0, 0.03]]
    samples = [
        vp.SpeakerSample(start_ms=i, end_ms=i + 1, embedding=vector)
        for i, vector in enumerate([*good, [0.0, 1.0]])
    ]
    centroid = vp.trimmed_centroid(samples)
    # 串进来的那一段应被剔除，质心仍然贴着前四段
    assert vp.cosine(centroid, [1.0, 0.0]) > 0.99
    assert vp.cosine(vp.mean_vector([s.embedding for s in samples]), [1.0, 0.0]) < 0.99


def test_cluster_candidates_merges_same_voice(_runtime_defaults: None):
    def candidate(cloud_id: str, vector: list[float]) -> vp.SpeakerCandidate:
        sample = vp.SpeakerSample(start_ms=0, end_ms=8000, embedding=vector, rms=0.2)
        item = vp.SpeakerCandidate(
            cloud_ids=[cloud_id], samples=[sample], talk_ms=20_000, segments=2
        )
        item.centroid = vector
        return item

    merged = vp.cluster_candidates(
        [
            candidate("c0:speaker_0", VOICE_A),
            candidate("c1:speaker_1", [0.99, 0.0, 0.03]),
            candidate("c0:speaker_1", VOICE_B),
        ]
    )
    assert len(merged) == 2
    biggest = merged[0]
    assert sorted(biggest.cloud_ids) == ["c0:speaker_0", "c1:speaker_1"]
    assert biggest.talk_ms == 40_000


# ---------- 假 ASR 下的整条链路 ----------


# 整场录音里谁在什么时候说话：(起, 止, 是不是甲, 内容)
SPEECH_PLAN = [
    (0.0, 20.0, True, "先过一下上周的进展。"),
    (21.0, 41.0, False, "接口那块还差联调。"),
    (900.0, 920.0, False, "联调今天能收尾。"),
    (921.0, 941.0, True, "那就按这个排期。"),
]


class Cuts:
    """记录 ffmpeg 切了哪一段到哪个文件，好让假 ASR 与假声纹知道自己在听什么。"""

    def __init__(self) -> None:
        self.by_name: dict[str, tuple[float, float]] = {}

    def remember(self, name: str, start: float, length: float) -> None:
        self.by_name[name] = (start, length)

    def lookup(self, url: str) -> tuple[float, float]:
        for name, span in self.by_name.items():
            if name in url:
                return span
        # 没切过说明送的是整段音轨
        return 0.0, TOTAL_SECONDS


class FakeAsrClient:
    """按送来的音频区间生成分句；每次调用的说话人编号独立，跟真接口一致。"""

    def __init__(
        self,
        cuts: Cuts,
        blocked: set[tuple[int, int]] | None = None,
        flaky: dict[tuple[int, int], int] | None = None,
    ) -> None:
        self._cuts = cuts
        # 命中这些区间（起, 长，取整秒）的调用会被「内容审核」一直拒掉
        self._blocked = blocked or set()
        # 这些区间只拒前 N 次，模拟内容审核抖动
        self._flaky = dict(flaky or {})
        self.calls: list[tuple[float, float]] = []

    async def transcribe(self, url: str, *, audio_format: str = "mp3") -> AsrResult:
        start, length = self._cuts.lookup(url)
        self.calls.append((start, length))
        span = (round(start), round(length))
        if self._flaky.get(span):
            self._flaky[span] -= 1
            raise AsrRequestError("转写任务失败 阶段=asr_call: risk blocked")
        if span in self._blocked:
            raise AsrRequestError("转写任务失败 阶段=asr_call: risk blocked")

        ids: dict[bool, str] = {}
        utterances: list[AsrUtterance] = []
        for begin, end, is_a, text in SPEECH_PLAN:
            if begin < start or end > start + length:
                continue
            # 云端只在单次调用内编号，谁先说谁是 speaker_0
            if is_a not in ids:
                ids[is_a] = f"speaker_{len(ids)}"
            utterances.append(
                AsrUtterance(
                    text=text,
                    start_ms=int((begin - start) * 1000),
                    end_ms=int((end - start) * 1000),
                    speaker_id=ids[is_a],
                )
            )
        return AsrResult(text="".join(u.text for u in utterances), utterances=utterances)


class FakeFfmpeg:
    """抽音轨只落占位文件；切片把绝对起点写进文件，供假声纹模型认人。"""

    class _Info:
        duration_seconds = TOTAL_SECONDS

    def __init__(self, cuts: Cuts, silences: list[tuple[float, float]] | None = None) -> None:
        self._cuts = cuts
        # 整场录音里的静音区间，探测时只返回落在窗口内的那些
        self._silences = silences if silences is not None else []

    async def detect_silence(self, media_path, start_seconds, duration_seconds, **kwargs):
        end = start_seconds + duration_seconds
        return [
            (max(a, start_seconds), min(b, end))
            for a, b in self._silences
            if b > start_seconds and a < end
        ]

    async def extract_audio(
        self, media_path, dest, *, start_seconds=None, duration_seconds=None, **kwargs
    ):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-audio")
        if start_seconds is not None:
            self._cuts.remember(dest.name, start_seconds, duration_seconds)
        return dest

    async def probe(self, media_path):
        return self._Info()

    async def extract_audio_clip(
        self, media_path, start_seconds, duration_seconds, dest, **kwargs
    ):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(str(start_seconds), encoding="utf-8")
        return dest


class FakeExtractor:
    """按片段的绝对起点在发言表里查这是谁，模拟真实模型的输出。"""

    def available(self) -> bool:
        return True

    def embed_file(self, wav_path: Path) -> tuple[list[float], float]:
        start = float(wav_path.read_text(encoding="utf-8"))
        for begin, end, is_a, _ in SPEECH_PLAN:
            if begin - 0.001 <= start < end:
                return (list(VOICE_A) if is_a else list(VOICE_B)), 0.2
        raise AssertionError(f"取样落在了没人说话的位置 {start}s")


class FakeR2:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def enabled(self) -> bool:
        return True

    def asr_audio_key(self, minute_token: str, *, owner_user_id: int) -> str:
        return f"{owner_user_id}/{minute_token}/audio.mp3"

    def asr_chunk_key(self, minute_token: str, index: int, *, owner_user_id: int) -> str:
        return f"{owner_user_id}/{minute_token}/chunk_{index:03d}.mp3"

    async def upload_asr_audio(self, path, key, *, minute_token: str) -> str:
        return key

    async def presign_asr_audio_key(self, key: str) -> str:
        return f"https://r2.example/{key}"

    async def delete_asr_audio(self, key: str) -> None:
        self.deleted.append(key)


@pytest.fixture
def _runtime_defaults() -> None:
    runtime_config.replace_cache(
        {
            "STEP_ASR_ENABLED": "true",
            "TRANSCRIPT_COVERAGE_THRESHOLD": "0.9",
            "SPEAKER_MATCH_THRESHOLD": "0.55",
            "SPEAKER_SAMPLE_COUNT": "1",
            "SPEAKER_SAMPLE_SECONDS": "8",
            "SPEAKER_MIN_SEGMENT_SECONDS": "12",
            "ASR_CHUNK_MINUTES": "15",
        }
    )
    yield
    runtime_config.replace_cache({})


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


class Rig:
    """一整套替身：R2、ffmpeg、声纹模型，外加记录切了哪些段。"""

    def __init__(self) -> None:
        self.cuts = Cuts()
        self.r2 = FakeR2()
        # 录音里的静音区间；留空则边界落在算术位置
        self.silences: list[tuple[float, float]] = []


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    item = Rig()
    ffmpeg = FakeFfmpeg(item.cuts, item.silences)
    monkeypatch.setattr("app.service.r2_media_service.r2_media_service", item.r2)
    monkeypatch.setattr("app.integrations.video.ffmpeg_client.ffmpeg_client", ffmpeg)
    monkeypatch.setattr("app.service.transcription_service.ffmpeg_client", ffmpeg)
    monkeypatch.setattr(vp, "extractor", FakeExtractor())
    return item


def _storage_with_media(tmp_path: Path, token: str) -> MeetingStorageService:
    storage = MeetingStorageService(storage_root=str(tmp_path))
    layout = storage.ensure_layout(token, owner_user_id=OWNER)
    (layout["media"] / "recording.mp4").write_bytes(b"fake-video")
    return storage


def _run_transcribe(storage: MeetingStorageService, token: str, asr: FakeAsrClient):
    service = TranscriptionService(storage=storage, asr_client=asr)
    return asyncio.run(
        service.transcribe(token, owner_user_id=OWNER, duration_ms=int(TOTAL_SECONDS * 1000))
    )


@pytest.mark.usefixtures("_runtime_defaults", "_memory_db")
def test_transcribe_merges_speakers_across_chunks(tmp_path: Path, rig: Rig):
    storage = _storage_with_media(tmp_path, TOKEN)

    result = _run_transcribe(storage, TOKEN, FakeAsrClient(rig.cuts))

    assert result.chunks == 2
    assert result.skipped_seconds == 0
    assert result.utterances == 4
    # 云端在两段里给了四个编号，声纹应把它们收敛回两个人
    assert len(result.speakers) == 2
    assert {s.local_label for s in result.speakers} == {"说话人1", "说话人2"}
    assert all(s.is_new for s in result.speakers)
    assert all(s.voiceprint_id is not None for s in result.speakers)

    # 每个人在两段里各说一次，共两段发言
    assert sorted(s.segments for s in result.speakers) == [2, 2]

    saved = (
        storage.get_meeting_dir(TOKEN, owner_user_id=OWNER)
        / "raw"
        / "transcript"
        / "transcript.asr.txt"
    )
    assert saved.is_file()
    assert saved.read_text(encoding="utf-8") == result.transcript

    # 同一个人的两次发言必须挂同一个编号，否则纪要会把一个人当两个人写
    lines = [ln for ln in result.transcript.splitlines() if ln.startswith("说话人")]
    assert len(lines) == 4
    assert lines[0].split()[0] == lines[3].split()[0]
    assert lines[1].split()[0] == lines[2].split()[0]
    assert lines[0].split()[0] != lines[1].split()[0]

    # 分段音频用完即删，只留整段的那份供试听
    assert len(rig.r2.deleted) == 2
    assert all("chunk_" in key for key in rig.r2.deleted)


@pytest.mark.usefixtures("_runtime_defaults")
def test_finished_transcription_forces_the_summary_to_rerun(monkeypatch):
    """转写重跑过，纪要必须跟着重跑：旧正文里的编号已经对不上新的说话人映射。"""
    from app.data_model.entity.pipeline_job import PipelineJobEntity
    from app.service import transcription_flow

    seen: dict[str, object] = {}

    async def fake_enqueue(token: str, **kwargs):
        seen.update(kwargs)
        seen["token"] = token
        return 1

    monkeypatch.setattr(transcription_flow, "enqueue_job", fake_enqueue)
    job = PipelineJobEntity(
        id=1,
        owner_user_id=OWNER,
        minute_token=TOKEN,
        job_type="TRANSCRIBE",
        mode=None,
        status="RUNNING",
        stage=None,
        percent=None,
        attempt=1,
        max_attempts=3,
        error_message=None,
        started_at=None,
        finished_at=None,
        broker_updated_at=None,
    )

    asyncio.run(transcription_flow._enqueue_summary(job, force=True))
    assert seen["mode"] == "FULL|force"

    # 转写失败那条路只是拿飞书那几分钟兜底，不该覆盖已有纪要
    asyncio.run(transcription_flow._enqueue_summary(job))
    assert seen["mode"] == "FULL"


def _speaker_candidate(cloud_id: str, centroid: list[float]) -> vp.SpeakerCandidate:
    item = vp.SpeakerCandidate(cloud_ids=[cloud_id], samples=[], talk_ms=0, segments=0)
    item.centroid = list(centroid)
    return item


def _verify(rig: Rig, tmp_path: Path, utterances: list[_ChunkUtterance]) -> int:
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake-audio")
    service = TranscriptionService(
        storage=MeetingStorageService(storage_root=str(tmp_path)),
        asr_client=FakeAsrClient(rig.cuts),
    )
    candidates = [
        _speaker_candidate("c1:speaker_0", VOICE_A),
        _speaker_candidate("c1:speaker_1", VOICE_B),
    ]
    return asyncio.run(service._verify_utterances(audio, utterances, candidates))


@pytest.mark.usefixtures("_runtime_defaults")
def test_verify_moves_a_line_that_sounds_like_someone_else(tmp_path: Path, rig: Rig):
    """云端把甲的话安给了乙，声纹应当把它改回来。"""
    # 900~920 这句实际是乙说的，却被标成了甲那一组
    stray = _ChunkUtterance(
        text="联调今天能收尾。",
        start_ms=900_000,
        end_ms=920_000,
        speaker_id="c1:speaker_0",
        chunk_index=0,
    )
    good = _ChunkUtterance(
        text="先过一下上周的进展。",
        start_ms=0,
        end_ms=20_000,
        speaker_id="c1:speaker_0",
        chunk_index=0,
    )

    assert _verify(rig, tmp_path, [good, stray]) == 1
    assert stray.speaker_id == "c1:speaker_1"
    # 本来就对的那句不许被动
    assert good.speaker_id == "c1:speaker_0"


@pytest.mark.usefixtures("_runtime_defaults")
def test_verify_skips_lines_too_short_to_judge(tmp_path: Path, rig: Rig):
    """一两秒的句子声纹不稳，宁可不判。"""
    brief = _ChunkUtterance(
        text="嗯。",
        start_ms=900_000,
        end_ms=901_500,
        speaker_id="c1:speaker_0",
        chunk_index=0,
    )

    assert _verify(rig, tmp_path, [brief]) == 0
    assert brief.speaker_id == "c1:speaker_0"


@pytest.mark.usefixtures("_runtime_defaults")
def test_verify_keeps_hands_off_when_the_gap_is_small(tmp_path: Path, rig: Rig):
    """两人质心都不像时差距不够大，保持原判。"""
    runtime_config.replace_cache({"SPEAKER_VERIFY_MARGIN": "1.5"})
    stray = _ChunkUtterance(
        text="联调今天能收尾。",
        start_ms=900_000,
        end_ms=920_000,
        speaker_id="c1:speaker_0",
        chunk_index=0,
    )

    assert _verify(rig, tmp_path, [stray]) == 0
    assert stray.speaker_id == "c1:speaker_0"


@pytest.mark.usefixtures("_runtime_defaults", "_memory_db")
def test_chunk_boundary_moves_to_the_nearest_silence(tmp_path: Path, rig: Rig):
    """硬切会把一句话劈成两半，边界附近有静音就挪过去，且挑最近的那处。"""
    rig.silences.extend([(886.0, 887.0), (893.0, 897.0)])
    storage = _storage_with_media(tmp_path, TOKEN)
    asr = FakeAsrClient(rig.cuts)

    result = _run_transcribe(storage, TOKEN, asr)

    spans = sorted((round(s, 1), round(ln, 1)) for s, ln in asr.calls)
    assert spans == [(0.0, 895.0), (895.0, 905.0)]
    assert result.utterances == 4
    assert len(result.speakers) == 2


@pytest.mark.usefixtures("_runtime_defaults", "_memory_db")
def test_boundary_stays_put_when_nothing_is_quiet(tmp_path: Path, rig: Rig):
    """整段都在说话就别硬找静音，老老实实按整数倍切。"""
    storage = _storage_with_media(tmp_path, TOKEN)
    asr = FakeAsrClient(rig.cuts)

    _run_transcribe(storage, TOKEN, asr)

    assert sorted((round(s), round(ln)) for s, ln in asr.calls) == [(0, 900), (900, 900)]


@pytest.mark.usefixtures("_runtime_defaults", "_memory_db")
def test_second_meeting_recognizes_named_speaker(tmp_path: Path, rig: Rig):
    first = _storage_with_media(tmp_path / "a", TOKEN)
    _run_transcribe(first, TOKEN, FakeAsrClient(rig.cuts))

    async def rename_first_person() -> int:
        async with UnitOfWork() as uow:
            people = await uow.voiceprints.list_active()
            speakers = await uow.voiceprints.list_speakers(TOKEN, owner_user_id=OWNER)
            target = next(s for s in speakers if s.local_label == "说话人1")
            await uow.voiceprints.update(
                VoiceprintUpdateEntity(id=target.voiceprint_id, display_name="张三")
            )
            await uow.commit()
            assert len(people) == 2
            return target.voiceprint_id

    named_id = asyncio.run(rename_first_person())

    second = _storage_with_media(tmp_path / "b", TOKEN2)
    result = _run_transcribe(second, TOKEN2, FakeAsrClient(rig.cuts))

    # 第二场不该再建人物，而且命过名的那位要带着名字回来
    assert all(not s.is_new for s in result.speakers)
    assert {s.display_name for s in result.speakers} == {"张三", None}
    matched = next(s for s in result.speakers if s.display_name == "张三")
    assert matched.voiceprint_id == named_id
    assert matched.match_score is not None and matched.match_score > 0.9

    async def count_people() -> int:
        async with UnitOfWork() as uow:
            return len(await uow.voiceprints.list_active())

    assert asyncio.run(count_people()) == 2

    # 展示层换名：文件里仍是编号，读出来才是真名
    assert "张三" not in result.transcript
    displayed = asyncio.run(
        speaker_naming_service.apply_speaker_names(
            result.transcript, TOKEN2, owner_user_id=OWNER
        )
    )
    assert "张三 " in displayed
    assert displayed.count("张三") == 2


@pytest.mark.usefixtures("_runtime_defaults", "_memory_db")
def test_flaky_rejection_is_retried_whole_before_splitting(tmp_path: Path, rig: Rig):
    """内容审核抖一下不该把整段切碎：切得越碎，云端说话人编号越对不上。"""
    storage = _storage_with_media(tmp_path, TOKEN)
    asr = FakeAsrClient(rig.cuts, flaky={(900, 900): 1})

    result = _run_transcribe(storage, TOKEN, asr)

    spans = [(round(s), round(ln)) for s, ln in asr.calls]
    assert spans.count((900, 900)) == 2
    assert not [span for span in spans if span[1] < 900]
    assert result.utterances == 4
    assert len(result.speakers) == 2


@pytest.mark.usefixtures("_runtime_defaults", "_memory_db")
def test_blocked_chunk_is_split_instead_of_failing_whole_meeting(
    tmp_path: Path, rig: Rig
):
    """一段被内容审核拒了不该毁掉整场：对半切开，只丢真正卡住的那一小块。"""
    storage = _storage_with_media(tmp_path, TOKEN)
    # 第二段整段（900s 起、900s 长）会被拒，切成两半后各自能过
    asr = FakeAsrClient(rig.cuts, blocked={(900, 900)})

    result = _run_transcribe(storage, TOKEN, asr)

    assert (900, 900) in [(round(s), round(ln)) for s, ln in asr.calls]
    assert (900, 450) in [(round(s), round(ln)) for s, ln in asr.calls]
    assert result.skipped_seconds == 0
    assert result.utterances == 4
    assert len(result.speakers) == 2


@pytest.mark.usefixtures("_runtime_defaults", "_memory_db")
def test_unrecoverable_stretch_is_skipped_with_the_rest_kept(
    tmp_path: Path, rig: Rig, monkeypatch: pytest.MonkeyPatch
):
    """切到最小粒度还是过不了，就丢这一小段，其余照常出转写。"""
    monkeypatch.setattr(
        "app.service.transcription_service.MIN_RETRY_SPLIT_SECONDS", 500.0
    )
    storage = _storage_with_media(tmp_path, TOKEN)
    asr = FakeAsrClient(rig.cuts, blocked={(900, 900), (900, 450), (1350, 450)})

    result = _run_transcribe(storage, TOKEN, asr)

    assert result.skipped_seconds == 900
    # 前一段的两句还在，说话人也只剩这两位
    assert result.utterances == 2
    assert len(result.speakers) == 2


@pytest.mark.usefixtures("_runtime_defaults", "_memory_db")
def test_transcribe_fails_when_media_missing(tmp_path: Path, rig: Rig):
    storage = MeetingStorageService(storage_root=str(tmp_path))
    storage.ensure_layout(TOKEN, owner_user_id=OWNER)

    with pytest.raises(TranscriptionError, match="音视频"):
        _run_transcribe(storage, TOKEN, FakeAsrClient(rig.cuts))


@pytest.mark.usefixtures("_runtime_defaults", "_memory_db")
def test_transcribe_requires_r2(tmp_path: Path, rig: Rig, monkeypatch: pytest.MonkeyPatch):
    class Disabled(FakeR2):
        def enabled(self) -> bool:
            return False

    monkeypatch.setattr("app.service.r2_media_service.r2_media_service", Disabled())
    storage = _storage_with_media(tmp_path, TOKEN)

    with pytest.raises(TranscriptionError, match="R2"):
        _run_transcribe(storage, TOKEN, FakeAsrClient(rig.cuts))
