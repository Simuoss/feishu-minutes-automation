"""本地导入的会议。

导入进来的会议之后要走同一条纪要流水线，所以这里盯的是「它能不能被当成一场正常
会议读出来」，以及那些飞书专属的操作有没有认出它并明确拒绝。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.db_base import Base
from app.repository.uow import UnitOfWork
from app.service import meeting_import_service as importer
from app.service import transcript_switch_service as switcher
from app.service.meeting_download_service import LOCAL_IMPORT_EVENT_TYPE
from app.service.meeting_storage_service import MeetingStorageService
from app.service.transcription_flow import (
    SOURCE_IMPORT,
    evaluate_transcript_coverage,
)

OWNER = 7


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


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """导入服务 + 拦下后续作业入队，本轮只看导入本身。"""
    storage = MeetingStorageService(storage_root=str(tmp_path / "meetings"))
    enqueued: list[str] = []

    async def fake_followups(minute_token: str, *, owner_user_id: int) -> None:
        enqueued.append(minute_token)

    monkeypatch.setattr(
        "app.service.meeting_download_service.MeetingDownloadService."
        "enqueue_followups",
        staticmethod(fake_followups),
    )
    monkeypatch.setattr(switcher, "_storage", storage)
    item = importer.MeetingImportService(storage=storage)
    return item, storage, enqueued


def _record(minute_token: str):
    async def load():
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            return await uow.meeting_records.get_latest_by_minute_token(
                minute_token, owner_user_id=OWNER
            )

    return asyncio.run(load())


def test_import_token_fits_every_column():
    token = importer.new_import_token()

    assert token.startswith("imp")
    # 各表的 minute_token 都是 VARCHAR(32)
    assert len(token) <= 32
    assert token.isalnum()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        (r"C:\Users\me\录音 1.m4a", "录音_1.m4a"),
        ("...", "import"),
    ],
)
def test_filename_is_stripped_of_paths_and_oddities(raw: str, expected: str):
    assert importer.sanitize_filename(raw) == expected


@pytest.mark.parametrize(
    ("filename", "kind"),
    [("a.mp4", "media"), ("a.m4a", "media"), ("a.docx", "text"), ("a.srt", "text")],
)
def test_classify_splits_media_from_documents(filename: str, kind: str):
    assert importer.classify(filename) == kind


def test_unsupported_type_is_rejected_with_a_readable_reason():
    with pytest.raises(importer.MeetingImportError, match="不支持的文件类型"):
        importer.classify("book.epub")


@pytest.mark.usefixtures("_memory_db")
def test_text_import_lands_as_a_readable_completed_meeting(
    tmp_path: Path, service
):
    svc, storage, enqueued = service
    source = tmp_path / "upload.bin"
    source.write_text("第一段。\n\n第二段。\n", encoding="utf-8")

    result = asyncio.run(
        svc.import_file(
            source,
            owner_user_id=OWNER,
            filename="会议纪要.txt",
            title="  ",
        )
    )

    # 标题留空就用文件名兜底
    assert result.title == "会议纪要"
    assert result.kind == "text"
    assert result.has_video is False
    assert source.exists() is False
    assert enqueued == [result.minute_token]

    transcript = storage.read_transcript(
        result.minute_token, owner_user_id=OWNER
    )
    assert "第一段。" in transcript

    record = _record(result.minute_token)
    # 不是 COMPLETED 的话 assert_meeting_readable 会让详情页整片 404
    assert record.status == "COMPLETED"
    assert record.event_type == LOCAL_IMPORT_EVENT_TYPE
    assert record.transcript_source == SOURCE_IMPORT
    assert record.title == "会议纪要"


@pytest.mark.usefixtures("_memory_db")
def test_media_import_records_probe_results(
    tmp_path: Path, service, monkeypatch: pytest.MonkeyPatch
):
    svc, storage, _ = service

    class Probe:
        duration_seconds = 42.5
        has_video_stream = False

    async def fake_probe(path: Path):
        return Probe()

    monkeypatch.setattr(importer.ffmpeg_client, "probe", fake_probe)
    source = tmp_path / "upload.bin"
    source.write_bytes(b"fake audio")

    result = asyncio.run(
        svc.import_file(
            source, owner_user_id=OWNER, filename="录音.m4a"
        )
    )

    assert result.kind == "media"
    assert result.duration_ms == 42_500
    # 容器是 m4a，探测也说没有视频流，就不该被当成有视频
    assert result.has_video is False
    assert storage.find_media_path(
        result.minute_token, owner_user_id=OWNER
    ).name == "录音.m4a"
    # 媒体导入不自带转写，来源留空等转写作业去写
    assert _record(result.minute_token).transcript_source is None


@pytest.mark.usefixtures("_memory_db")
def test_unprobeable_media_still_imports(
    tmp_path: Path, service, monkeypatch: pytest.MonkeyPatch
):
    """探测失败只影响时长展示，不该把整次导入挡回去。"""
    svc, _, _ = service

    async def boom(path: Path):
        raise importer.FfmpegError("ffprobe 不在")

    monkeypatch.setattr(importer.ffmpeg_client, "probe", boom)
    source = tmp_path / "upload.bin"
    source.write_bytes(b"fake video")

    result = asyncio.run(
        svc.import_file(source, owner_user_id=OWNER, filename="录屏.mp4")
    )

    assert result.duration_ms is None
    # 探不出来时按扩展名猜，mp4 当成有视频
    assert result.has_video is True


@pytest.mark.usefixtures("_memory_db")
def test_broken_document_does_not_leave_a_half_meeting(
    tmp_path: Path, service
):
    svc, _, enqueued = service
    source = tmp_path / "upload.bin"
    source.write_text("   \n", encoding="utf-8")

    with pytest.raises(importer.MeetingImportError):
        asyncio.run(
            svc.import_file(source, owner_user_id=OWNER, filename="空.txt")
        )

    assert enqueued == []


@pytest.mark.usefixtures("_memory_db")
def test_imported_text_meeting_is_never_sent_to_asr(tmp_path: Path, service):
    """纯文本没有段首时间戳，硬算覆盖率会得出 0 并误判成被截断。"""
    svc, _, _ = service
    source = tmp_path / "upload.bin"
    source.write_text("就一段话。\n", encoding="utf-8")

    result = asyncio.run(
        svc.import_file(source, owner_user_id=OWNER, filename="纪要.txt")
    )
    _, needs_asr = asyncio.run(
        evaluate_transcript_coverage(
            result.minute_token, owner_user_id=OWNER
        )
    )

    assert needs_asr is False


@pytest.mark.usefixtures("_memory_db")
def test_imported_meeting_cannot_switch_back_to_feishu(
    tmp_path: Path, service
):
    """飞书上根本没有这场妙记，切回去只会走到一半才发现拉不到。"""
    svc, _, _ = service
    source = tmp_path / "upload.bin"
    source.write_text("就一段话。\n", encoding="utf-8")

    result = asyncio.run(
        svc.import_file(source, owner_user_id=OWNER, filename="纪要.txt")
    )

    with pytest.raises(switcher.TranscriptSwitchError, match="本地导入"):
        asyncio.run(
            switcher.switch_to_feishu(
                result.minute_token, owner_user_id=OWNER
            )
        )
