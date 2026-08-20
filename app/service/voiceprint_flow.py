"""声纹提炼作业的入队闸门。

单独一层是为了让入队条件只有一处：下载收尾、单场手动触发、列表批量补跑都走这里。
"""

from __future__ import annotations

import logging

from app.core import runtime_config
from app.repository.uow import UnitOfWork
from app.service import voiceprint_service as vp
from app.service.meeting_storage_service import (
    ASR_TRANSCRIPT_FILENAME,
    MeetingStorageService,
)
from app.service.pipeline_queue import JOB_VOICEPRINT, enqueue_job
from app.service.transcription_flow import SOURCE_FEISHU

logger = logging.getLogger(__name__)

_storage = MeetingStorageService()


async def voiceprint_harvest_blocker(
    minute_token: str, *, owner_user_id: int
) -> str | None:
    """能不能对这场会议做声纹提炼；不能则返回给人看的原因。"""
    if not runtime_config.get_bool("VOICEPRINT_HARVEST_ENABLED", True):
        return "声纹自动提炼已关闭"
    if not vp.extractor.available():
        return "声纹模型不可用，请确认已安装 sherpa-onnx 且模型文件已下载"

    from app.service.r2_media_service import r2_media_service

    if not r2_media_service.enabled():
        return "声纹提炼要先把音轨放到公网直链上，请先启用 R2 存储"

    transcript_dir = (
        _storage.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
        / "raw"
        / "transcript"
    )
    has_feishu_text = transcript_dir.is_dir() and any(
        f.is_file() and f.name != ASR_TRANSCRIPT_FILENAME
        for f in transcript_dir.iterdir()
    )
    if not has_feishu_text:
        return "这场会议没有飞书原文转写，姓名无从取得"

    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        record = await uow.meeting_records.get_latest_by_minute_token(
            minute_token, owner_user_id=owner_user_id
        )
    source = record.transcript_source if record else None
    if source and source != SOURCE_FEISHU:
        # 自建转写用的是「说话人N」，没有真名可蹭
        return "当前生效的是自建转写，没有可用的真实姓名"
    return None


async def enqueue_voiceprint(
    minute_token: str, *, owner_user_id: int
) -> int | None:
    blocker = await voiceprint_harvest_blocker(
        minute_token, owner_user_id=owner_user_id
    )
    if blocker is not None:
        logger.info(
            "跳过声纹提炼入队 token=%s 原因=%s", minute_token, blocker
        )
        return None
    return await enqueue_job(
        minute_token,
        owner_user_id=owner_user_id,
        job_type=JOB_VOICEPRINT,
    )
