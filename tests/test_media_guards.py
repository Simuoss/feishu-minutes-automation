"""音频会议不走配图、文字会议不走 ASR。

这两类会议以前不只是白跑一趟，还各会在作业列表里留一条 FAILED，看着像坏了其实
只是这场会议没有那个东西。这里守住「不入队」与「跳过而非失败」两条线。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core import runtime_config
from app.data_model.entity.pipeline_job import PipelineJobEntity
from app.service import meeting_download_service as download
from app.service import pipeline_worker as worker_module
from app.service.meeting_storage_service import MeetingStorageService
from app.service.pipeline_queue import (
    JOB_SHARE_VIDEO,
    STATUS_COMPLETED,
    STATUS_FAILED,
)

OWNER = 7
TOKEN = "obguard000001"


def _job() -> PipelineJobEntity:
    return PipelineJobEntity(
        id=1,
        owner_user_id=OWNER,
        minute_token=TOKEN,
        job_type=JOB_SHARE_VIDEO,
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


def _storage_with(tmp_path: Path, filename: str) -> MeetingStorageService:
    storage = MeetingStorageService(storage_root=str(tmp_path))
    layout = storage.ensure_layout(TOKEN, owner_user_id=OWNER)
    (layout["media"] / filename).write_bytes(b"media")
    return storage


@pytest.fixture
def storage_with_audio(tmp_path: Path) -> MeetingStorageService:
    return _storage_with(tmp_path, "recording.m4a")


def _followup_job_types(
    storage: MeetingStorageService, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """跑一遍收尾入队，回报排了哪些作业类型。"""
    enqueued: list[str] = []

    class FakeR2:
        @staticmethod
        def enabled() -> bool:
            return True

    async def fake_enqueue(minute_token, *, owner_user_id, job_type, **kwargs):
        enqueued.append(job_type)
        return 1

    async def fake_coverage(minute_token, *, owner_user_id):
        return 1.0, False

    async def fake_voiceprint(minute_token, *, owner_user_id):
        return None

    monkeypatch.setattr(
        download, "MeetingStorageService", lambda *a, **k: storage
    )
    monkeypatch.setattr("app.service.r2_media_service.r2_media_service", FakeR2())
    monkeypatch.setattr("app.service.pipeline_queue.enqueue_job", fake_enqueue)
    monkeypatch.setattr(
        "app.service.transcription_flow.evaluate_transcript_coverage",
        fake_coverage,
    )
    monkeypatch.setattr(
        "app.service.voiceprint_flow.enqueue_voiceprint", fake_voiceprint
    )
    runtime_config.replace_cache({"SUMMARY_AUTO_GENERATE": "false"})
    try:
        asyncio.run(
            download.MeetingDownloadService.enqueue_followups(
                TOKEN, owner_user_id=OWNER
            )
        )
    finally:
        runtime_config.replace_cache({})
    return enqueued


def test_audio_only_meeting_is_not_queued_for_a_share_clip(
    storage_with_audio: MeetingStorageService, monkeypatch: pytest.MonkeyPatch
):
    """没有视频就没有分享片可压，排了只会失败。"""
    assert JOB_SHARE_VIDEO not in _followup_job_types(
        storage_with_audio, monkeypatch
    )


def test_video_meeting_still_gets_a_share_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    storage = _storage_with(tmp_path, "recording.mp4")

    assert JOB_SHARE_VIDEO in _followup_job_types(storage, monkeypatch)


def test_share_clip_job_without_video_finishes_as_skipped(
    monkeypatch: pytest.MonkeyPatch,
):
    """「本来就没视频」和「压缩上传真失败」得分开报。"""
    updates: list[dict] = []

    class FakeR2:
        @staticmethod
        async def ensure_share_video(minute_token, *, owner_user_id, on_progress):
            return False

        @staticmethod
        def has_local_video(minute_token, *, owner_user_id) -> bool:
            return False

    async def fake_update(job_id, **fields):
        updates.append(fields)

    monkeypatch.setattr(worker_module, "r2_media_service", FakeR2())
    monkeypatch.setattr(worker_module, "update_job", fake_update)

    asyncio.run(worker_module.PipelineWorker()._run_share_video(_job()))

    assert updates[-1]["status"] == STATUS_COMPLETED
    assert updates[-1]["stage"] == "SKIPPED_NO_VIDEO"


def test_share_clip_job_with_video_still_reports_real_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    updates: list[dict] = []

    class FakeR2:
        @staticmethod
        async def ensure_share_video(minute_token, *, owner_user_id, on_progress):
            return False

        @staticmethod
        def has_local_video(minute_token, *, owner_user_id) -> bool:
            return True

    async def fake_update(job_id, **fields):
        updates.append(fields)

    monkeypatch.setattr(worker_module, "r2_media_service", FakeR2())
    monkeypatch.setattr(worker_module, "update_job", fake_update)

    asyncio.run(worker_module.PipelineWorker()._run_share_video(_job()))

    assert updates[-1]["status"] == STATUS_FAILED


def test_audio_only_meeting_skips_illustration_without_touching_r2(
    storage_with_audio: MeetingStorageService, monkeypatch: pytest.MonkeyPatch
):
    """纯音频会议去 R2 兜一圈是白跑，还会让进度条先跳再退。"""
    from app.service.summary_generation_service import SummaryGenerationService

    service = SummaryGenerationService(storage=storage_with_audio)

    async def explode(*args, **kwargs):
        raise AssertionError("纯音频会议不该碰 R2")

    monkeypatch.setattr(
        "app.service.summary_generation_service.r2_media_service.ensure_local_video",
        explode,
    )
    runtime_config.replace_cache({"SUMMARY_ILLUSTRATE": "true"})
    try:
        figures = asyncio.run(
            service._prepare_figures(
                TOKEN,
                "说话人1 00:00:00.000 \n聊了点什么\n",
                scene=None,
                owner_user_id=OWNER,
            )
        )
    finally:
        runtime_config.replace_cache({})

    assert figures == []
