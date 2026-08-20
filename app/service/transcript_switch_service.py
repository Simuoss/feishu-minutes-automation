"""手动在飞书自带转写与自建 ASR 之间切换。

自动流程只在飞书那份被截断时才转自建，但有时人工判断更准：飞书转写虽然覆盖全程
却错字连篇，或者反过来自建识别把专有名词念歪了。这里把两个方向都做成显式操作。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core import runtime_config
from app.core.config import settings
from app.data_model.entity.meeting_record import MeetingRecordUpdateEntity
from app.repository.uow import UnitOfWork
from app.service.meeting_storage_service import (
    ASR_TRANSCRIPT_FILENAME,
    MeetingStorageService,
)
from app.service.pipeline_queue import (
    JOB_SUMMARY,
    JOB_TRANSCRIBE,
    STATUS_QUEUED,
    STATUS_RUNNING,
    encode_job_mode,
    enqueue_job,
)
from app.service.transcription_flow import (
    SOURCE_ASR,
    SOURCE_FEISHU,
    can_self_transcribe,
    enqueue_transcribe,
    refresh_speakers_json,
)

logger = logging.getLogger(__name__)

_storage = MeetingStorageService()


class TranscriptSwitchError(RuntimeError):
    """切换转写来源失败，消息直接给前端看。"""


@dataclass
class SwitchResult:
    source: str
    job_id: int | None
    message: str


def resolve_current_source(record, minute_token: str, *, owner_user_id: int) -> str:
    """当前生效的转写来源。

    DB 字段可能为空（旧数据、或判定过但还没跑），落盘的文件才是读路径的真相。
    """
    if record is not None and record.transcript_source:
        return str(record.transcript_source)
    asr_file = (
        _storage.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
        / "raw"
        / "transcript"
        / ASR_TRANSCRIPT_FILENAME
    )
    return SOURCE_ASR if asr_file.is_file() else SOURCE_FEISHU


async def get_source(minute_token: str, *, owner_user_id: int) -> str:
    record = await _load_record(minute_token, owner_user_id=owner_user_id)
    return resolve_current_source(
        record, minute_token, owner_user_id=owner_user_id
    )


async def switch_to_asr(minute_token: str, *, owner_user_id: int) -> SwitchResult:
    """改用自建 ASR 重跑。音轨不在了会回飞书重下，那一步在转写作业里做。"""
    from app.service.r2_media_service import r2_media_service

    if not r2_media_service.enabled():
        raise TranscriptSwitchError(
            "自建转写要把音频放到公网直链上，请先启用 R2 存储"
        )
    if not runtime_config.get_bool("STEP_ASR_ENABLED", settings.step_asr_enabled):
        raise TranscriptSwitchError("自建转写已被关闭，请先在系统配置里打开")

    await _reject_if_running(minute_token, owner_user_id=owner_user_id)
    if not await can_self_transcribe(minute_token, owner_user_id=owner_user_id):
        raise TranscriptSwitchError("这场会议没有可用的音频，无法自建转写")

    record = await _load_record(minute_token, owner_user_id=owner_user_id)
    if record is not None and record.id is not None:
        # 置空而不是直接写 asr：转写真跑成了才算，失败时覆盖率判定还能重来
        await _update_record(record.id, transcript_source=None)

    job_id = await enqueue_transcribe(
        minute_token, owner_user_id=owner_user_id
    )
    logger.info(
        "手动切到自建转写 token=%s owner=%s job=%s",
        minute_token,
        owner_user_id,
        job_id,
    )
    return SwitchResult(
        source=SOURCE_ASR,
        job_id=job_id,
        message="已排队自建转写，跑完会自动重新生成纪要",
    )


async def switch_to_feishu(minute_token: str, *, owner_user_id: int) -> SwitchResult:
    """改回飞书自带转写。删掉自建那份，缺飞书原文就重下一份。"""
    from app.service.meeting_download_service import (
        LOCAL_IMPORT_EVENT_TYPE,
        MeetingDownloadService,
    )

    record = await _load_record(minute_token, owner_user_id=owner_user_id)
    if record is not None and (record.event_type or "") == LOCAL_IMPORT_EVENT_TYPE:
        raise TranscriptSwitchError(
            "这是本地导入的会议，飞书上没有对应妙记，没有转写可切回"
        )
    await _reject_if_running(minute_token, owner_user_id=owner_user_id)

    transcript_dir = (
        _storage.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
        / "raw"
        / "transcript"
    )
    asr_file = transcript_dir / ASR_TRANSCRIPT_FILENAME
    asr_file.unlink(missing_ok=True)

    has_feishu_text = any(
        f.is_file()
        and f.name != ASR_TRANSCRIPT_FILENAME
        and f.suffix.lower() in {".txt", ".srt"}
        for f in (transcript_dir.iterdir() if transcript_dir.is_dir() else [])
    )
    if not has_feishu_text:
        logger.info(
            "本地没有飞书原文，重下一份 token=%s owner=%s",
            minute_token,
            owner_user_id,
        )
        result = await MeetingDownloadService().download_minute(
            minute_token,
            skip_if_completed=False,
            redownload_transcript=True,
            owner_user_id=owner_user_id,
        )
        if result.status == "FAILED":
            raise TranscriptSwitchError(
                f"重新下载飞书转写失败：{result.error_message or '未知原因'}"
            )

    # 自建那套「说话人N」编号连同样本一起作废，留着只会让展示层贴错名字
    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        await uow.voiceprints.clear_speakers(
            minute_token, owner_user_id=owner_user_id
        )
        await uow.voiceprints.clear_samples_of_meeting(
            minute_token, owner_user_id=owner_user_id
        )
        await uow.commit()

    if record is not None and record.id is not None:
        await _update_record(record.id, transcript_source=SOURCE_FEISHU)
    await refresh_speakers_json(minute_token, owner_user_id=owner_user_id)

    job_id = await enqueue_job(
        minute_token,
        owner_user_id=owner_user_id,
        job_type=JOB_SUMMARY,
        mode=encode_job_mode("FULL", force=True),
    )
    logger.info(
        "手动切回飞书转写 token=%s owner=%s summary_job=%s",
        minute_token,
        owner_user_id,
        job_id,
    )
    return SwitchResult(
        source=SOURCE_FEISHU,
        job_id=job_id,
        message="已切回飞书转写，正在重新生成纪要",
    )


async def _reject_if_running(minute_token: str, *, owner_user_id: int) -> None:
    """已经有转写在跑就别再排一条，静默去重会让用户以为没反应。"""
    async with UnitOfWork() as uow:
        assert uow.pipeline_jobs is not None
        jobs = await uow.pipeline_jobs.list_by_minute_token(
            minute_token, owner_user_id=owner_user_id
        )
    for job in jobs:
        if job.job_type == JOB_TRANSCRIBE and job.status in {
            STATUS_QUEUED,
            STATUS_RUNNING,
        }:
            raise TranscriptSwitchError(
                "这场会议已经有自建转写在排队或进行中，等它跑完再切"
            )


async def _load_record(minute_token: str, *, owner_user_id: int):
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        return await uow.meeting_records.get_latest_by_minute_token(
            minute_token, owner_user_id=owner_user_id
        )


async def _update_record(record_id: int, **fields) -> None:
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        await uow.meeting_records.update(
            MeetingRecordUpdateEntity(id=record_id, **fields)
        )
        await uow.commit()
