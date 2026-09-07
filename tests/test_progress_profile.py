"""进度条按会议场景拆成不同模式，不再共用一套配图阶段。"""

import asyncio
from pathlib import Path

import pytest

from app.data_model.entity.meeting_record import MeetingRecordCreateEntity
from app.repository.uow import UnitOfWork
from app.service.meeting_download_service import LOCAL_IMPORT_EVENT_TYPE
from app.service.meeting_storage_service import MeetingStorageService
from app.service.progress_profile import (
    classify_progress_profile,
    detect_media_kind,
    resolve_progress_profile,
)
from app.service.transcription_flow import SOURCE_ASR, SOURCE_FEISHU, SOURCE_IMPORT

OWNER = 9
TOKEN = "obprofile0001"


def test_import_text_skips_transcribe_and_figures():
    profile = classify_progress_profile(
        event_type=LOCAL_IMPORT_EVENT_TYPE,
        transcript_source=SOURCE_IMPORT,
        media_kind="text",
    )
    assert profile.id == "import_text"
    assert profile.label == "上传文字"
    assert profile.has_transcribe is False
    assert profile.has_figures is False


def test_import_audio_transcribes_without_figures():
    profile = classify_progress_profile(
        event_type=LOCAL_IMPORT_EVENT_TYPE,
        transcript_source=SOURCE_ASR,
        media_kind="audio",
    )
    assert profile.id == "import_audio"
    assert profile.has_transcribe is True
    assert profile.has_figures is False


def test_import_video_has_transcribe_and_figures():
    profile = classify_progress_profile(
        event_type=LOCAL_IMPORT_EVENT_TYPE,
        transcript_source=None,
        media_kind="video",
    )
    assert profile.id == "import_video"
    assert profile.has_transcribe is True
    assert profile.has_figures is True


def test_feishu_full_video_has_figures_no_transcribe():
    profile = classify_progress_profile(
        event_type="vc.meeting.meeting_ended_v1",
        transcript_source=SOURCE_FEISHU,
        media_kind="video",
    )
    assert profile.id == "feishu_full"
    assert profile.has_transcribe is False
    assert profile.has_figures is True


def test_feishu_partial_uses_transcribe_job_or_asr_source():
    by_source = classify_progress_profile(
        event_type="vc.meeting.meeting_ended_v1",
        transcript_source=SOURCE_ASR,
        media_kind="video",
    )
    by_inflight = classify_progress_profile(
        event_type="vc.meeting.meeting_ended_v1",
        transcript_source=SOURCE_FEISHU,
        media_kind="video",
        transcribe_inflight=True,
    )
    assert by_source.id == "feishu_partial"
    assert by_inflight.id == "feishu_partial"
    assert by_source.has_transcribe is True
    assert by_source.has_figures is True


def test_feishu_audio_keeps_label_but_drops_figures():
    full = classify_progress_profile(
        event_type="vc.meeting.meeting_ended_v1",
        transcript_source=SOURCE_FEISHU,
        media_kind="audio",
    )
    partial = classify_progress_profile(
        event_type="vc.meeting.meeting_ended_v1",
        transcript_source=SOURCE_ASR,
        media_kind="audio",
    )
    assert full.id == "feishu_full"
    assert full.has_figures is False
    assert partial.id == "feishu_partial"
    assert partial.has_transcribe is True
    assert partial.has_figures is False


def test_detect_media_kind_prefers_local_files():
    assert (
        detect_media_kind(
            video_path=Path("a.mp4"),
            media_path=Path("a.mp4"),
            has_video=False,
            event_type=LOCAL_IMPORT_EVENT_TYPE,
            transcript_source=None,
        )
        == "video"
    )
    assert (
        detect_media_kind(
            video_path=None,
            media_path=Path("talk.m4a"),
            has_video=None,
            event_type=LOCAL_IMPORT_EVENT_TYPE,
            transcript_source=None,
        )
        == "audio"
    )
    assert (
        detect_media_kind(
            video_path=None,
            media_path=None,
            has_video=None,
            event_type=LOCAL_IMPORT_EVENT_TYPE,
            transcript_source=SOURCE_IMPORT,
        )
        == "text"
    )


def test_detect_media_kind_uses_record_when_files_gone():
    assert (
        detect_media_kind(
            video_path=None,
            media_path=None,
            has_video=True,
            event_type="vc.meeting.meeting_ended_v1",
            transcript_source=SOURCE_FEISHU,
        )
        == "video"
    )
    assert (
        detect_media_kind(
            video_path=None,
            media_path=None,
            has_video=False,
            event_type="vc.meeting.meeting_ended_v1",
            transcript_source=SOURCE_FEISHU,
        )
        == "audio"
    )


@pytest.mark.usefixtures("_memory_db")
def test_resolve_import_audio_from_storage(tmp_path: Path):
    storage = MeetingStorageService(storage_root=str(tmp_path))
    layout = storage.ensure_layout(TOKEN, owner_user_id=OWNER)
    (layout["media"] / "talk.m4a").write_bytes(b"audio")

    async def _run():
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            await uow.meeting_records.create(
                MeetingRecordCreateEntity(
                    feishu_event_id=f"seed-{TOKEN}",
                    event_type=LOCAL_IMPORT_EVENT_TYPE,
                    minute_token=TOKEN,
                    status="COMPLETED",
                    storage_path="whatever",
                    owner_user_id=OWNER,
                    has_video=False,
                    transcript_source=SOURCE_ASR,
                )
            )
            await uow.commit()
        return await resolve_progress_profile(
            TOKEN,
            owner_user_id=OWNER,
            transcribe_inflight=True,
            storage=storage,
        )

    profile = asyncio.run(_run())
    assert profile.id == "import_audio"
    assert profile.has_transcribe is True
    assert profile.has_figures is False
