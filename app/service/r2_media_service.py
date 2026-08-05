"""会议媒体 R2 双写与分享签名：配图同步、分享视频压缩上传、presign。"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from app.core.config import settings
from app.integrations.r2_client import R2ClientError, r2_client
from app.integrations.video.ffmpeg_client import FfmpegError, ffmpeg_client
from app.service.meeting_storage_service import MeetingStorageService
from app.service.time_utils import utc_now_ms

logger = logging.getLogger(__name__)

VIDEO_STATUS_PENDING = "PENDING"
VIDEO_STATUS_READY = "READY"
VIDEO_STATUS_FAILED = "FAILED"

_r2_video_tasks: set[asyncio.Task[object]] = set()
_video_locks: dict[str, asyncio.Lock] = {}


class R2MediaService:
    def __init__(self, storage: MeetingStorageService | None = None) -> None:
        self._storage = storage or MeetingStorageService()

    def enabled(self) -> bool:
        return r2_client.configured

    def asset_key(self, minute_token: str, filename: str) -> str:
        name = Path(unquote(filename)).name
        return f"meetings/{minute_token}/assets/{name}"

    def share_video_key(self, minute_token: str) -> str:
        return f"meetings/{minute_token}/share/video.mp4"

    def _meta_path(self, minute_token: str) -> Path:
        return self._storage.get_meeting_dir(minute_token) / "output" / "r2.meta.json"

    def read_meta(self, minute_token: str) -> dict[str, Any]:
        path = self._meta_path(minute_token)
        if not path.is_file():
            return {"assets": {}, "video": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "读取 r2.meta.json 失败 token=%s，怀疑写入中断，将按空状态继续",
                minute_token,
            )
            return {"assets": {}, "video": {}}
        if not isinstance(data, dict):
            return {"assets": {}, "video": {}}
        assets = data.get("assets") if isinstance(data.get("assets"), dict) else {}
        video = data.get("video") if isinstance(data.get("video"), dict) else {}
        return {"assets": assets, "video": video}

    def write_meta(self, minute_token: str, meta: dict[str, Any]) -> None:
        self._storage.ensure_layout(minute_token)
        path = self._meta_path(minute_token)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def video_ready(self, minute_token: str) -> bool:
        video = self.read_meta(minute_token).get("video") or {}
        return video.get("status") == VIDEO_STATUS_READY and bool(video.get("key"))

    def asset_recorded(self, minute_token: str, filename: str) -> bool:
        name = Path(unquote(filename)).name
        assets = self.read_meta(minute_token).get("assets") or {}
        item = assets.get(name)
        return isinstance(item, dict) and bool(item.get("key"))

    async def sync_assets(self, minute_token: str) -> int:
        """把本地 output/assets 中尚未记录的文件上传到 R2。"""
        if not self.enabled():
            return 0
        assets_dir = self._storage.get_assets_dir(minute_token)
        if not assets_dir.is_dir():
            return 0

        meta = self.read_meta(minute_token)
        assets_meta: dict[str, Any] = dict(meta.get("assets") or {})
        uploaded = 0
        for path in sorted(assets_dir.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            existing = assets_meta.get(name)
            if isinstance(existing, dict) and existing.get("key"):
                continue
            key = self.asset_key(minute_token, name)
            content_type = mimetypes.guess_type(name)[0] or "image/jpeg"
            try:
                etag = await r2_client.upload_file(path, key, content_type=content_type)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "配图上传 R2 失败 token=%s file=%s，分享页将回退本地: %s",
                    minute_token,
                    name,
                    exc,
                )
                continue
            assets_meta[name] = {
                "key": key,
                "etag": etag,
                "uploaded_at": utc_now_ms(),
            }
            uploaded += 1

        if uploaded:
            meta["assets"] = assets_meta
            self.write_meta(minute_token, meta)
            logger.info("配图已同步到 R2 token=%s count=%s", minute_token, uploaded)
        return uploaded

    async def ensure_asset_on_r2(self, minute_token: str, filename: str) -> str | None:
        """确保单张配图在 R2，返回 object key；失败返回 None。"""
        if not self.enabled():
            return None
        name = Path(unquote(filename)).name
        meta = self.read_meta(minute_token)
        assets_meta: dict[str, Any] = dict(meta.get("assets") or {})
        existing = assets_meta.get(name)
        if isinstance(existing, dict) and existing.get("key"):
            return str(existing["key"])

        local = self._storage.resolve_asset_path(minute_token, name)
        if local is None:
            return None
        key = self.asset_key(minute_token, name)
        content_type = mimetypes.guess_type(name)[0] or "image/jpeg"
        try:
            etag = await r2_client.upload_file(local, key, content_type=content_type)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "按需上传配图到 R2 失败 token=%s file=%s，将回退本地文件: %s",
                minute_token,
                name,
                exc,
            )
            return None
        assets_meta[name] = {
            "key": key,
            "etag": etag,
            "uploaded_at": utc_now_ms(),
        }
        meta["assets"] = assets_meta
        self.write_meta(minute_token, meta)
        return key

    async def presign_asset(self, minute_token: str, filename: str) -> str | None:
        key = await self.ensure_asset_on_r2(minute_token, filename)
        if not key:
            return None
        try:
            return await r2_client.presign_get(key)
        except R2ClientError as exc:
            logger.error(
                "配图签名 URL 生成失败 token=%s file=%s，分享将回退本地: %s",
                minute_token,
                filename,
                exc,
            )
            return None

    async def presign_share_video(self, minute_token: str) -> str | None:
        if not self.enabled() or not self.video_ready(minute_token):
            return None
        key = (self.read_meta(minute_token).get("video") or {}).get("key")
        if not key:
            return None
        try:
            return await r2_client.presign_get(str(key))
        except R2ClientError as exc:
            logger.error(
                "分享视频签名 URL 生成失败 token=%s，将回退本地媒体接口: %s",
                minute_token,
                exc,
            )
            return None

    def _video_lock(self, minute_token: str) -> asyncio.Lock:
        lock = _video_locks.get(minute_token)
        if lock is None:
            lock = asyncio.Lock()
            _video_locks[minute_token] = lock
        return lock

    async def ensure_share_video(self, minute_token: str) -> None:
        """压缩本地视频并上传 R2（幂等；已 READY 则跳过）。"""
        if not self.enabled():
            return
        async with self._video_lock(minute_token):
            meta = self.read_meta(minute_token)
            video_meta = dict(meta.get("video") or {})
            if video_meta.get("status") == VIDEO_STATUS_READY and video_meta.get("key"):
                return

            source = self._storage.find_video_path(minute_token)
            if source is None:
                logger.info(
                    "妙记无本地视频，跳过 R2 分享压缩 token=%s",
                    minute_token,
                )
                return

            video_meta = {
                "status": VIDEO_STATUS_PENDING,
                "key": self.share_video_key(minute_token),
                "updated_at": utc_now_ms(),
                "error": None,
            }
            meta["video"] = video_meta
            self.write_meta(minute_token, meta)

            tmp_path: Path | None = None
            try:
                with tempfile.TemporaryDirectory(prefix="r2-share-") as tmp:
                    tmp_path = Path(tmp) / "share.mp4"
                    await ffmpeg_client.compress_for_share(source, tmp_path)
                    etag = await r2_client.upload_file(
                        tmp_path,
                        video_meta["key"],
                        content_type="video/mp4",
                    )
                meta = self.read_meta(minute_token)
                meta["video"] = {
                    "status": VIDEO_STATUS_READY,
                    "key": video_meta["key"],
                    "etag": etag,
                    "updated_at": utc_now_ms(),
                    "error": None,
                    "source_name": source.name,
                }
                self.write_meta(minute_token, meta)
                logger.info("分享压缩视频已上传 R2 token=%s", minute_token)
            except (FfmpegError, R2ClientError, OSError) as exc:
                meta = self.read_meta(minute_token)
                meta["video"] = {
                    "status": VIDEO_STATUS_FAILED,
                    "key": video_meta.get("key"),
                    "updated_at": utc_now_ms(),
                    "error": str(exc),
                }
                self.write_meta(minute_token, meta)
                logger.error(
                    "分享视频压缩/上传 R2 失败 token=%s，分享页将继续用本地原片: %s",
                    minute_token,
                    exc,
                )

    def spawn_share_video(self, minute_token: str) -> None:
        if not self.enabled():
            return
        task: asyncio.Task[object] = asyncio.create_task(
            self.ensure_share_video(minute_token)
        )
        _r2_video_tasks.add(task)
        task.add_done_callback(_r2_video_tasks.discard)
        logger.info("已排队压缩并上传分享视频到 R2 token=%s", minute_token)

    async def sync_assets_safe(self, minute_token: str) -> None:
        try:
            await self.sync_assets(minute_token)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "纪要配图同步 R2 失败 token=%s，不影响纪要落盘，分享页可回退本地: %s",
                minute_token,
                exc,
            )


r2_media_service = R2MediaService()
