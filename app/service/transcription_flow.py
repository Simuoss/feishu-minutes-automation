"""自建转写作业的编排：判定要不要转、跑转写、收尾再放纪要进队。

判定与作业执行放在一起，是因为两边共用同一套「覆盖率」口径：飞书免费版妙记只
转前几分钟，末条时间戳会远早于录音结束，这就是识别这类会议的依据。
"""

from __future__ import annotations

import asyncio
import json
import logging

from app.core import runtime_config
from app.data_model.entity.meeting_record import MeetingRecordUpdateEntity
from app.data_model.entity.pipeline_job import PipelineJobEntity
from app.repository.uow import UnitOfWork
from app.service import speaker_naming_service
from app.service.meeting_storage_service import MeetingStorageService
from app.service.pipeline_job_service import update_job
from app.service.pipeline_queue import (
    JOB_SUMMARY,
    JOB_TRANSCRIBE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    encode_job_mode,
    enqueue_job,
)
from app.service.transcript_anchor import extract_unique_speakers
from app.service.transcription_service import (
    TranscriptionError,
    needs_self_transcription,
    transcript_coverage,
    transcription_service,
)

logger = logging.getLogger(__name__)

SOURCE_FEISHU = "feishu"
SOURCE_ASR = "asr"
# 本地导入带进来的转写：既不是飞书导出的，也不是自建识别的
SOURCE_IMPORT = "import"

# 发言少于这个时长的说话人不进参会人列表，多半是分离出来的碎片
MIN_PARTICIPANT_TALK_MS = 5_000

_storage = MeetingStorageService()


def has_media_for_asr(minute_token: str, *, owner_user_id: int) -> bool:
    """本地有没有可转写的媒体。自动流程用这个便宜的判断，不擅自触发下载。"""
    return (
        _storage.find_media_path(minute_token, owner_user_id=owner_user_id)
        is not None
    )


async def can_self_transcribe(minute_token: str, *, owner_user_id: int) -> bool:
    """这场会议究竟有没有办法拿到音频。

    比 has_media_for_asr 宽：原片被清理过的飞书会议还能重下一份，只有本地导入的
    纯文本会议是真的无路可走。手动切换走这个判断。
    """
    from app.integrations.r2_client import r2_client
    from app.service.meeting_download_service import LOCAL_IMPORT_EVENT_TYPE
    from app.service.r2_media_service import r2_media_service

    if has_media_for_asr(minute_token, owner_user_id=owner_user_id):
        return True
    if r2_media_service.enabled():
        key = r2_media_service.asr_audio_key(
            minute_token, owner_user_id=owner_user_id
        )
        if await r2_client.exists(key):
            return True
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        record = await uow.meeting_records.get_latest_by_minute_token(
            minute_token, owner_user_id=owner_user_id
        )
    return record is not None and (
        record.event_type or ""
    ) != LOCAL_IMPORT_EVENT_TYPE


async def evaluate_transcript_coverage(
    minute_token: str, *, owner_user_id: int
) -> tuple[float | None, bool]:
    """算这场会议的转写覆盖率，并判断是否需要自建转写。"""
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        record = await uow.meeting_records.get_latest_by_minute_token(
            minute_token, owner_user_id=owner_user_id
        )
    duration_ms = record.duration_ms if record else None
    if record and record.transcript_source and record.transcript_source != SOURCE_FEISHU:
        # 覆盖率这套口径只对飞书导出的转写成立。自建转写已经覆盖全程；本地导入的
        # 纯文本没有段首时间戳，硬算会得出 0 并误判成「被截断」。
        return record.transcript_coverage, False

    text = await _storage.read_transcript_async(
        minute_token, owner_user_id=owner_user_id
    )
    coverage = transcript_coverage(text, duration_ms)
    if coverage is None:
        logger.info(
            "无法判定转写覆盖率 token=%s（缺时长或缺转写），按飞书转写继续",
            minute_token,
        )
        return None, False
    decision = needs_self_transcription(coverage)
    if decision and not has_media_for_asr(minute_token, owner_user_id=owner_user_id):
        # 纯文本会议、或只下了转写没下媒体的会议：硬排转写只会攒一条失败作业
        logger.info(
            "转写覆盖率只有 %.2f，但本地没有音视频，无法自建转写，沿用现有转写 token=%s",
            coverage,
            minute_token,
        )
        decision = False
    logger.info(
        "转写覆盖率 token=%s coverage=%.2f 时长=%.0fs 判定=%s",
        minute_token,
        coverage,
        (duration_ms or 0) / 1000,
        "自建转写" if decision else "沿用飞书转写",
    )
    if record and record.id is not None:
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            await uow.meeting_records.update(
                MeetingRecordUpdateEntity(
                    id=record.id,
                    transcript_coverage=coverage,
                    transcript_source=None if decision else SOURCE_FEISHU,
                )
            )
            await uow.commit()
    return coverage, decision


async def refresh_speakers_json(minute_token: str, *, owner_user_id: int) -> list[str]:
    """按当前生效的转写（已换过真名）刷新参会人列表，供列表页筛选。"""
    text = await _storage.read_transcript_async(
        minute_token, owner_user_id=owner_user_id
    )
    speakers = extract_unique_speakers(text) if text else []
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        assert uow.voiceprints is not None
        rows = await uow.voiceprints.list_speakers(
            minute_token, owner_user_id=owner_user_id
        )
        if rows:
            # 云端分离偶尔会甩出只说一两句的碎片，把它们摆进参会人筛选里没有意义
            keep = {
                row.local_label
                for row in rows
                if row.talk_ms >= MIN_PARTICIPANT_TALK_MS
            }
            names = await speaker_naming_service.speaker_name_map(
                minute_token, owner_user_id=owner_user_id
            )
            filtered = [names.get(label, label) for label in keep]
            speakers = [name for name in speakers if name in set(filtered)]
        record = await uow.meeting_records.get_latest_by_minute_token(
            minute_token, owner_user_id=owner_user_id
        )
        if record and record.id is not None:
            await uow.meeting_records.update(
                MeetingRecordUpdateEntity(
                    id=record.id,
                    speakers_json=json.dumps(speakers, ensure_ascii=False),
                )
            )
            await uow.commit()
    return speakers


async def run_transcribe_job(job: PipelineJobEntity) -> None:
    assert job.id is not None
    loop = asyncio.get_running_loop()

    def on_progress(stage: str, percent: float) -> None:
        asyncio.run_coroutine_threadsafe(
            update_job(job.id, stage=stage, percent=percent), loop
        )

    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        record = await uow.meeting_records.get_latest_by_minute_token(
            job.minute_token, owner_user_id=job.owner_user_id
        )

    await update_job(job.id, stage="准备音频", percent=4.0)
    try:
        result = await transcription_service.transcribe(
            job.minute_token,
            owner_user_id=job.owner_user_id,
            duration_ms=record.duration_ms if record else None,
            create_time=record.create_time if record else None,
            on_progress=on_progress,
        )
    except TranscriptionError as exc:
        logger.error(
            "自建转写失败 token=%s owner=%s: %s",
            job.minute_token,
            job.owner_user_id,
            exc,
        )
        await update_job(
            job.id,
            status=STATUS_FAILED,
            stage="FAILED",
            error_message=str(exc),
            finished=True,
        )
        # 转写没成，但飞书那几分钟还在，仍然让纪要跑一版，总比什么都没有强
        await _enqueue_summary(job)
        return

    if record and record.id is not None:
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            await uow.meeting_records.update(
                MeetingRecordUpdateEntity(
                    id=record.id,
                    transcript_source=SOURCE_ASR,
                )
            )
            await uow.commit()
    speakers = await refresh_speakers_json(
        job.minute_token, owner_user_id=job.owner_user_id
    )

    named = sum(1 for item in result.speakers if item.display_name)
    logger.info(
        "自建转写完成 token=%s 分句=%d 说话人=%d（已认出 %d 位）参会人=%s",
        job.minute_token,
        result.utterances,
        len(result.speakers),
        named,
        speakers,
    )
    if result.skipped_seconds > 0:
        logger.warning(
            "自建转写有 %.0fs 内容识别不出（多半触了内容审核），纪要会缺这一段。token=%s",
            result.skipped_seconds,
            job.minute_token,
        )
    await update_job(
        job.id,
        status=STATUS_COMPLETED,
        stage="DONE",
        percent=100.0,
        finished=True,
    )
    # 必须强制重跑：说话人编号是这次转写重排出来的，旧纪要正文里的编号已经对不上
    await _enqueue_summary(job, force=True)


async def _enqueue_summary(job: PipelineJobEntity, *, force: bool = False) -> None:
    if not runtime_config.get_bool("SUMMARY_AUTO_GENERATE", True):
        return
    await enqueue_job(
        job.minute_token,
        owner_user_id=job.owner_user_id,
        job_type=JOB_SUMMARY,
        mode=encode_job_mode("FULL", force=force),
    )


async def enqueue_transcribe(minute_token: str, *, owner_user_id: int) -> int | None:
    """入队自建转写。手动切换与批量补跑都走这里，闸门也就放在这里。"""
    if not await can_self_transcribe(minute_token, owner_user_id=owner_user_id):
        raise TranscriptionError("这场会议没有可用的音频，无法自建转写")
    return await enqueue_job(
        minute_token,
        owner_user_id=owner_user_id,
        job_type=JOB_TRANSCRIBE,
    )
