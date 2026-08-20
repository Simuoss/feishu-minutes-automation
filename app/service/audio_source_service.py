"""保证一场会议有可用的 16k 单声道音轨。

自建转写与声纹提炼都要吃这份音轨，而原片在纪要完成且分享片上云后就被删了，
所以取音轨要能从便宜到贵地回落：本地现成的 -> R2 上现成的 -> 本地媒体现抽 ->
回飞书重下原片。返回时云上和本地都有，云上那份给识别接口签直链，本地那份用来
切片。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.integrations.video.ffmpeg_client import FfmpegError, ffmpeg_client
from app.service.meeting_storage_service import MeetingStorageService

logger = logging.getLogger(__name__)

_storage = MeetingStorageService()


class AudioSourceError(RuntimeError):
    """拿不到这场会议的音轨。"""


@dataclass
class AudioSource:
    key: str
    local_path: Path
    # 本轮为了拿音轨而重下的原片，用完该由调用方决定是否清掉
    downloaded_media: bool = False


async def ensure_asr_audio(
    minute_token: str,
    *,
    owner_user_id: int,
    storage: MeetingStorageService | None = None,
    allow_refetch: bool = True,
) -> AudioSource:
    """确保这场会议的音轨在 R2 和本地都有。

    allow_refetch=False 时不回飞书重下，只用现成的东西。
    """
    from app.service.r2_media_service import r2_media_service

    if not r2_media_service.enabled():
        raise AudioSourceError("音轨需要放到公网直链上，当前未启用 R2 存储")

    store = storage or _storage
    key = r2_media_service.asr_audio_key(
        minute_token, owner_user_id=owner_user_id
    )
    work_dir = store.get_asr_work_dir(minute_token, owner_user_id=owner_user_id)
    audio_path = work_dir / "audio.mp3"

    if audio_path.is_file() and audio_path.stat().st_size > 0:
        if not await r2_media_service.asr_audio_exists(key):
            await r2_media_service.upload_asr_audio(
                audio_path, key, minute_token=minute_token
            )
        return AudioSource(key=key, local_path=audio_path)

    media = store.find_media_path(minute_token, owner_user_id=owner_user_id)
    if media is None and await r2_media_service.asr_audio_exists(key):
        logger.info("本地没有原片，把 R2 上的音轨拉回来复用 token=%s", minute_token)
        await r2_media_service.download_asr_audio(key, audio_path)
        return AudioSource(key=key, local_path=audio_path)

    downloaded = False
    if media is None:
        if not allow_refetch:
            raise AudioSourceError("本地没有音视频且不允许重新下载，拿不到音轨")
        media = await _refetch_media(
            minute_token, owner_user_id=owner_user_id, storage=store
        )
        downloaded = True

    try:
        await ffmpeg_client.extract_audio(media, audio_path)
    except FfmpegError as exc:
        raise AudioSourceError(f"抽取音轨失败：{exc}") from exc

    await r2_media_service.upload_asr_audio(
        audio_path, key, minute_token=minute_token
    )
    logger.info(
        "音轨已就绪 token=%s size=%.1fMB 来源=%s",
        minute_token,
        audio_path.stat().st_size / 1024 / 1024,
        "重下原片" if downloaded else "本地媒体",
    )
    return AudioSource(
        key=key, local_path=audio_path, downloaded_media=downloaded
    )


async def _refetch_media(
    minute_token: str, *, owner_user_id: int, storage: MeetingStorageService
) -> Path:
    """本地原片已被清理时回飞书重下一份。"""
    from app.service.meeting_download_service import (
        LOCAL_IMPORT_EVENT_TYPE,
        MeetingDownloadService,
    )
    from app.repository.uow import UnitOfWork

    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        record = await uow.meeting_records.get_latest_by_minute_token(
            minute_token, owner_user_id=owner_user_id
        )
    if record is not None and (record.event_type or "") == LOCAL_IMPORT_EVENT_TYPE:
        raise AudioSourceError(
            "这是本地导入的会议，飞书上没有对应原片，无法重新下载"
        )

    logger.info(
        "本地原片已不在，回飞书重下以取音轨 token=%s owner=%s",
        minute_token,
        owner_user_id,
    )
    result = await MeetingDownloadService().download_minute(
        minute_token,
        skip_if_completed=False,
        redownload_media=True,
        owner_user_id=owner_user_id,
    )
    if result.status == "FAILED":
        raise AudioSourceError(
            f"重新下载原片失败：{result.error_message or '未知原因'}"
        )
    media = storage.find_media_path(minute_token, owner_user_id=owner_user_id)
    if media is None:
        raise AudioSourceError("重新下载完成但本地仍找不到媒体文件")
    return media


def cleanup_refetched_media(
    source: AudioSource, minute_token: str, *, owner_user_id: int
) -> None:
    """本轮为取音轨而重下的原片用完就删，别把磁盘撑满。"""
    if not source.downloaded_media:
        return
    from app.service.r2_media_service import r2_media_service

    removed = r2_media_service.purge_local_videos(
        minute_token, owner_user_id=owner_user_id
    )
    logger.info(
        "取完音轨已清理重下的原片 token=%s removed=%s", minute_token, removed
    )
