"""会议媒体 R2 同步与签名：配图/分享视频上传成功后删除本地副本。"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from app.core.config import settings
from app.integrations.r2_client import R2ClientError, r2_client
from app.integrations.video.ffmpeg_client import FfmpegError, ffmpeg_client
from app.service.meeting_storage_service import MeetingStorageService, VIDEO_EXTENSIONS
from app.service.time_utils import utc_now_ms

logger = logging.getLogger(__name__)

VIDEO_STATUS_PENDING = "PENDING"
VIDEO_STATUS_READY = "READY"
VIDEO_STATUS_FAILED = "FAILED"

_r2_video_tasks: set[asyncio.Task[object]] = set()
_video_locks: dict[str, asyncio.Lock] = {}
_meta_locks: dict[str, asyncio.Lock] = {}


class R2MediaService:
    def __init__(self, storage: MeetingStorageService | None = None) -> None:
        self._storage = storage or MeetingStorageService()

    def enabled(self) -> bool:
        return r2_client.configured

    def asset_key(self, minute_token: str, filename: str, *, owner_user_id: int) -> str:
        name = Path(unquote(filename)).name
        return f"meetings/{owner_user_id}/{minute_token}/assets/{name}"

    def share_video_key(self, minute_token: str, *, owner_user_id: int) -> str:
        return f"meetings/{owner_user_id}/{minute_token}/share/video.mp4"

    def summary_key(self, minute_token: str, *, owner_user_id: int) -> str:
        return f"meetings/{owner_user_id}/{minute_token}/text/summary.md"

    def transcript_key(
        self,
        minute_token: str,
        *,
        owner_user_id: int,
        filename_suffix: str = "txt",
    ) -> str:
        suffix = (filename_suffix or "txt").strip().lstrip(".") or "txt"
        return f"meetings/{owner_user_id}/{minute_token}/text/transcript.{suffix}"

    def _assert_owned_object_key(
        self, key: str, *, owner_user_id: int, minute_token: str
    ) -> str:
        """拒绝 DB meta 里指向他人前缀的脏 key，防止跨租户预签名。

        允许两种前缀：
        - 新：meetings/{owner}/{token}/...
        - 旧：meetings/{token}/...（磁盘/R2 迁 owner 前上传的对象）
        """
        normalized = str(key or "").strip().lstrip("/")
        if not normalized or ".." in normalized:
            logger.error(
                "R2 object key 非法，拒绝签名/拉取。owner=%s token=%s key=%s",
                owner_user_id,
                minute_token,
                key,
            )
            raise ValueError("R2 object key 非法")
        owned_prefix = f"meetings/{owner_user_id}/{minute_token}/"
        legacy_prefix = f"meetings/{minute_token}/"
        if normalized.startswith(owned_prefix) or normalized.startswith(
            legacy_prefix
        ):
            return normalized
        logger.error(
            "R2 object key 不属于当前会议，拒绝签名/拉取。"
            "owner=%s token=%s key=%s",
            owner_user_id,
            minute_token,
            key,
        )
        raise ValueError("R2 object key 与会议归属不一致")

    def _r2_cache_dir(self, minute_token: str, *, owner_user_id: int) -> Path:
        path = (
            self._storage.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "agent"
            / "r2-cache"
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _normalize_meta(data: dict[str, Any] | None) -> dict[str, Any]:
        if not data:
            return {"assets": {}, "video": {}, "text": {}}
        assets = data.get("assets") if isinstance(data.get("assets"), dict) else {}
        video = data.get("video") if isinstance(data.get("video"), dict) else {}
        text = data.get("text") if isinstance(data.get("text"), dict) else {}
        return {"assets": assets, "video": video, "text": text}

    async def read_meta_async(
        self, minute_token: str, *, owner_user_id: int
    ) -> dict[str, Any]:
        from app.service.metadata_db_service import read_r2_meta

        try:
            data = await read_r2_meta(minute_token, owner_user_id=owner_user_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "从 DB 读取 R2 同步状态失败 token=%s，将按空状态继续。err=%s",
                minute_token,
                exc,
            )
            return {"assets": {}, "video": {}, "text": {}}
        return self._normalize_meta(data)

    def read_meta(self, minute_token: str, *, owner_user_id: int) -> dict[str, Any]:
        """同步门面：供 worker 使用；async 路由请用 read_meta_async。"""
        from app.core.async_bridge import run_async

        return run_async(
            self.read_meta_async(minute_token, owner_user_id=owner_user_id)
        )

    def _meta_lock(self, minute_token: str, *, owner_user_id: int) -> asyncio.Lock:
        lock_key = f"{owner_user_id}:{minute_token}"
        lock = _meta_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            _meta_locks[lock_key] = lock
        return lock

    async def write_meta_async(
        self, minute_token: str, meta: dict[str, Any], *, owner_user_id: int
    ) -> None:
        """合并写入 R2 同步状态，避免并发上传配图时互相覆盖 assets 登记。"""
        from app.service.metadata_db_service import read_r2_meta, upsert_r2_meta

        incoming = self._normalize_meta(meta)
        async with self._meta_lock(minute_token, owner_user_id=owner_user_id):
            try:
                current = self._normalize_meta(
                    await read_r2_meta(minute_token, owner_user_id=owner_user_id)
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "合并写入前读取 R2 状态失败 token=%s，将按本次写入继续。err=%s",
                    minute_token,
                    exc,
                )
                current = {"assets": {}, "video": {}, "text": {}}
            merged = {
                "assets": {**current["assets"], **incoming["assets"]},
                "text": {**current["text"], **incoming["text"]},
                "video": (
                    incoming["video"]
                    if incoming["video"]
                    else current["video"]
                ),
            }
            # 显式更新 video 字段时以本次为准（含 FAILED/READY）
            if incoming["video"]:
                merged["video"] = {**current["video"], **incoming["video"]}
            try:
                await upsert_r2_meta(
                    minute_token, merged, owner_user_id=owner_user_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "写入 R2 同步状态到 DB 失败 token=%s，后续可能重复上传。err=%s",
                    minute_token,
                    exc,
                )
                raise

    def write_meta(
        self, minute_token: str, meta: dict[str, Any], *, owner_user_id: int
    ) -> None:
        from app.core.async_bridge import run_async

        run_async(
            self.write_meta_async(
                minute_token, meta, owner_user_id=owner_user_id
            )
        )

    def video_ready(self, minute_token: str, *, owner_user_id: int) -> bool:
        video = self.read_meta(minute_token, owner_user_id=owner_user_id).get("video") or {}
        return video.get("status") == VIDEO_STATUS_READY and bool(video.get("key"))

    async def video_ready_async(
        self, minute_token: str, *, owner_user_id: int
    ) -> bool:
        meta = await self.read_meta_async(
            minute_token, owner_user_id=owner_user_id
        )
        video = meta.get("video") or {}
        return video.get("status") == VIDEO_STATUS_READY and bool(video.get("key"))

    def asset_recorded(
        self, minute_token: str, filename: str, *, owner_user_id: int
    ) -> bool:
        name = Path(unquote(filename)).name
        assets = self.read_meta(minute_token, owner_user_id=owner_user_id).get("assets") or {}
        item = assets.get(name)
        return isinstance(item, dict) and bool(item.get("key"))

    @staticmethod
    def _unlink_quiet(path: Path, *, what: str, minute_token: str) -> None:
        try:
            if path.is_file():
                path.unlink()
                logger.info("已删除本地%s token=%s path=%s", what, minute_token, path.name)
        except OSError as exc:
            logger.error(
                "删除本地%s失败 token=%s path=%s，云上已有副本但磁盘占用未释放。err=%s",
                what,
                minute_token,
                path,
                exc,
            )

    def purge_local_asset(
        self, minute_token: str, filename: str, *, owner_user_id: int
    ) -> None:
        path = self._storage.resolve_asset_path(
            minute_token, filename, owner_user_id=owner_user_id
        )
        if path is not None:
            self._unlink_quiet(path, what="配图", minute_token=minute_token)

    def purge_local_assets_dir(self, minute_token: str, *, owner_user_id: int) -> int:
        assets_dir = self._storage.get_assets_dir(
            minute_token, owner_user_id=owner_user_id
        )
        if not assets_dir.is_dir():
            return 0
        meta = self.read_meta(minute_token, owner_user_id=owner_user_id)
        assets_meta = meta.get("assets") or {}
        deleted = 0
        for path in list(assets_dir.iterdir()):
            if not path.is_file():
                continue
            recorded = assets_meta.get(path.name)
            if isinstance(recorded, dict) and recorded.get("key"):
                self._unlink_quiet(path, what="配图", minute_token=minute_token)
                deleted += 1
        return deleted

    def purge_local_videos(self, minute_token: str, *, owner_user_id: int) -> int:
        """删除 raw/media 下视频及临时缓存（仅在 R2 视频 READY 后调用）。"""
        deleted = 0
        media_dir = (
            self._storage.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "raw"
            / "media"
        )
        if media_dir.is_dir():
            for path in list(media_dir.iterdir()):
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                    self._unlink_quiet(path, what="原片视频", minute_token=minute_token)
                    deleted += 1
        cache_video = self._r2_cache_dir(
            minute_token, owner_user_id=owner_user_id
        ) / "video.mp4"
        if cache_video.is_file():
            self._unlink_quiet(cache_video, what="视频缓存", minute_token=minute_token)
            deleted += 1
        # 历史预压缩目录
        pre = (
            self._storage.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "agent"
            / "r2-compress-test"
            / "share.mp4"
        )
        if pre.is_file():
            self._unlink_quiet(pre, what="预压缩视频", minute_token=minute_token)
            deleted += 1
        return deleted

    async def sync_assets(self, minute_token: str, *, owner_user_id: int) -> int:
        """把本地 output/assets 上传到 R2；先落库登记再删本地，避免并发覆盖丢登记。"""
        if not self.enabled():
            return 0
        assets_dir = self._storage.get_assets_dir(
            minute_token, owner_user_id=owner_user_id
        )
        if not assets_dir.is_dir():
            return 0

        uploaded = 0
        for path in sorted(assets_dir.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            meta = await self.read_meta_async(
                minute_token, owner_user_id=owner_user_id
            )
            existing = (meta.get("assets") or {}).get(name)
            if isinstance(existing, dict) and existing.get("key"):
                self._unlink_quiet(path, what="配图", minute_token=minute_token)
                continue
            key = self.asset_key(minute_token, name, owner_user_id=owner_user_id)
            content_type = mimetypes.guess_type(name)[0] or "image/jpeg"
            try:
                etag = await r2_client.upload_file(path, key, content_type=content_type)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "配图上传 R2 失败 token=%s file=%s，将保留本地文件以便重试: %s",
                    minute_token,
                    name,
                    exc,
                )
                continue
            await self.write_meta_async(
                minute_token,
                {
                    "assets": {
                        name: {
                            "key": key,
                            "etag": etag,
                            "uploaded_at": utc_now_ms(),
                        }
                    }
                },
                owner_user_id=owner_user_id,
            )
            uploaded += 1
            self._unlink_quiet(path, what="配图", minute_token=minute_token)

        if uploaded:
            logger.info(
                "配图已同步到 R2 并清理本地 token=%s count=%s",
                minute_token,
                uploaded,
            )
        return uploaded

    async def ensure_asset_on_r2(
        self, minute_token: str, filename: str, *, owner_user_id: int
    ) -> str | None:
        """确保单张配图在 R2，返回 object key；失败返回 None。"""
        if not self.enabled():
            return None
        name = Path(unquote(filename)).name
        meta = self.read_meta(minute_token, owner_user_id=owner_user_id)
        assets_meta: dict[str, Any] = dict(meta.get("assets") or {})
        existing = assets_meta.get(name)
        if isinstance(existing, dict) and existing.get("key"):
            try:
                key = self._assert_owned_object_key(
                    str(existing["key"]),
                    owner_user_id=owner_user_id,
                    minute_token=minute_token,
                )
            except ValueError:
                return None
            self.purge_local_asset(
                minute_token, name, owner_user_id=owner_user_id
            )
            return key

        local = self._storage.resolve_asset_path(
            minute_token, name, owner_user_id=owner_user_id
        )
        key = self.asset_key(minute_token, name, owner_user_id=owner_user_id)
        if local is None:
            # 本地已删但对象仍在 R2、仅 DB 登记被并发覆盖时：按规范 key 探测并补登记
            if await r2_client.exists(key):
                await self.write_meta_async(
                    minute_token,
                    {
                        "assets": {
                            name: {
                                "key": key,
                                "etag": None,
                                "uploaded_at": utc_now_ms(),
                            }
                        }
                    },
                    owner_user_id=owner_user_id,
                )
                logger.warning(
                    "配图本地缺失但 R2 仍有对象，已补回 DB 登记 token=%s file=%s",
                    minute_token,
                    name,
                )
                return key
            return None
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
        await self.write_meta_async(
            minute_token,
            {
                "assets": {
                    name: {
                        "key": key,
                        "etag": etag,
                        "uploaded_at": utc_now_ms(),
                    }
                }
            },
            owner_user_id=owner_user_id,
        )
        self._unlink_quiet(local, what="配图", minute_token=minute_token)
        return key

    async def materialize_asset(
        self, minute_token: str, filename: str, *, owner_user_id: int
    ) -> Path | None:
        """导出/处理需要本地文件时，优先本地，否则从 R2 拉到缓存。"""
        name = Path(unquote(filename)).name
        local = self._storage.resolve_asset_path(
            minute_token, name, owner_user_id=owner_user_id
        )
        if local is not None:
            return local
        if not self.enabled():
            return None
        meta = self.read_meta(minute_token, owner_user_id=owner_user_id)
        item = (meta.get("assets") or {}).get(name)
        if not isinstance(item, dict) or not item.get("key"):
            return None
        try:
            object_key = self._assert_owned_object_key(
                str(item["key"]),
                owner_user_id=owner_user_id,
                minute_token=minute_token,
            )
        except ValueError:
            return None
        cache = (
            self._r2_cache_dir(minute_token, owner_user_id=owner_user_id)
            / "assets"
            / name
        )
        if cache.is_file():
            return cache
        try:
            await r2_client.download_file(object_key, cache)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "从 R2 拉取配图失败 token=%s file=%s，导出/复核将缺图。err=%s",
                minute_token,
                name,
                exc,
            )
            return None
        return cache

    async def ensure_local_video(
        self, minute_token: str, *, owner_user_id: int
    ) -> Path | None:
        """抽帧等处理需要视频时：本地原片优先，否则从 R2 分享片拉缓存。"""
        local = self._storage.find_video_path(
            minute_token, owner_user_id=owner_user_id
        )
        if local is not None:
            return local
        if not self.enabled() or not self.video_ready(
            minute_token, owner_user_id=owner_user_id
        ):
            return None
        key = (
            self.read_meta(minute_token, owner_user_id=owner_user_id).get("video") or {}
        ).get("key")
        if not key:
            return None
        try:
            object_key = self._assert_owned_object_key(
                str(key),
                owner_user_id=owner_user_id,
                minute_token=minute_token,
            )
        except ValueError:
            return None
        cache = self._r2_cache_dir(
            minute_token, owner_user_id=owner_user_id
        ) / "video.mp4"
        if cache.is_file():
            return cache
        try:
            await r2_client.download_file(object_key, cache)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "从 R2 拉取分享视频失败 token=%s，无法抽帧配图。err=%s",
                minute_token,
                exc,
            )
            return None
        return cache

    async def presign_asset(
        self, minute_token: str, filename: str, *, owner_user_id: int
    ) -> str | None:
        key = await self.ensure_asset_on_r2(
            minute_token, filename, owner_user_id=owner_user_id
        )
        if not key:
            # 本地已删、仅 DB 有记录时 ensure 可能因本地缺失早退——再读一次 meta
            name = Path(unquote(filename)).name
            item = (
                self.read_meta(minute_token, owner_user_id=owner_user_id).get("assets")
                or {}
            ).get(name)
            if isinstance(item, dict) and item.get("key"):
                try:
                    key = self._assert_owned_object_key(
                        str(item["key"]),
                        owner_user_id=owner_user_id,
                        minute_token=minute_token,
                    )
                except ValueError:
                    return None
            else:
                return None
        try:
            key = self._assert_owned_object_key(
                str(key),
                owner_user_id=owner_user_id,
                minute_token=minute_token,
            )
            return await r2_client.presign_get(key)
        except ValueError:
            return None
        except R2ClientError as exc:
            logger.error(
                "配图签名 URL 生成失败 token=%s file=%s: %s",
                minute_token,
                filename,
                exc,
            )
            return None

    async def presign_share_video(
        self, minute_token: str, *, owner_user_id: int
    ) -> str | None:
        if not self.enabled() or not self.video_ready(
            minute_token, owner_user_id=owner_user_id
        ):
            return None
        key = (
            self.read_meta(minute_token, owner_user_id=owner_user_id).get("video") or {}
        ).get("key")
        if not key:
            return None
        try:
            object_key = self._assert_owned_object_key(
                str(key),
                owner_user_id=owner_user_id,
                minute_token=minute_token,
            )
            return await r2_client.presign_get(object_key)
        except ValueError:
            return None
        except R2ClientError as exc:
            logger.error(
                "分享视频签名 URL 生成失败 token=%s: %s",
                minute_token,
                exc,
            )
            return None

    def _video_lock(self, minute_token: str, *, owner_user_id: int) -> asyncio.Lock:
        lock_key = f"{owner_user_id}:{minute_token}"
        lock = _video_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            _video_locks[lock_key] = lock
        return lock

    async def ensure_share_video(
        self, minute_token: str, *, owner_user_id: int
    ) -> bool:
        """压缩本地视频并上传 R2。成功返回 True；不在此处删除原片。"""
        if not self.enabled():
            return False
        async with self._video_lock(minute_token, owner_user_id=owner_user_id):
            meta = self.read_meta(minute_token, owner_user_id=owner_user_id)
            video_meta = dict(meta.get("video") or {})
            if video_meta.get("status") == VIDEO_STATUS_READY and video_meta.get("key"):
                return True

            source = self._storage.find_video_path(
                minute_token, owner_user_id=owner_user_id
            )
            if source is None:
                logger.info(
                    "妙记无本地视频，跳过 R2 分享压缩 token=%s",
                    minute_token,
                )
                return False

            video_meta = {
                "status": VIDEO_STATUS_PENDING,
                "key": self.share_video_key(
                    minute_token, owner_user_id=owner_user_id
                ),
                "updated_at": utc_now_ms(),
                "error": None,
            }
            meta["video"] = video_meta
            self.write_meta(minute_token, meta, owner_user_id=owner_user_id)

            tmp_root: Path | None = None
            try:
                tmp_root = Path(tempfile.mkdtemp(prefix="r2-share-"))
                tmp_path = tmp_root / "share.mp4"
                await ffmpeg_client.compress_for_share(source, tmp_path)
                # Windows 上 ffmpeg 偶发仍短暂占用输出文件，稍候再读上传
                await asyncio.sleep(0.2)
                etag = await r2_client.upload_file(
                    tmp_path,
                    video_meta["key"],
                    content_type="video/mp4",
                )
                meta = self.read_meta(minute_token, owner_user_id=owner_user_id)
                meta["video"] = {
                    "status": VIDEO_STATUS_READY,
                    "key": video_meta["key"],
                    "etag": etag,
                    "updated_at": utc_now_ms(),
                    "error": None,
                    "source_name": source.name,
                }
                self.write_meta(minute_token, meta, owner_user_id=owner_user_id)
                logger.info(
                    "分享压缩视频已上传 R2 token=%s（原片保留至纪要完成后清理）",
                    minute_token,
                )
                return True
            except (FfmpegError, R2ClientError, OSError) as exc:
                meta = self.read_meta(minute_token, owner_user_id=owner_user_id)
                meta["video"] = {
                    "status": VIDEO_STATUS_FAILED,
                    "key": video_meta.get("key"),
                    "updated_at": utc_now_ms(),
                    "error": str(exc),
                }
                self.write_meta(minute_token, meta, owner_user_id=owner_user_id)
                logger.error(
                    "分享视频压缩/上传 R2 失败 token=%s，保留本地原片以便重试: %s",
                    minute_token,
                    exc,
                )
                return False
            finally:
                if tmp_root is not None:
                    await self._rmtree_retry(tmp_root)

    @staticmethod
    async def _rmtree_retry(path: Path, *, attempts: int = 6) -> None:
        """删除临时目录；Windows 上文件句柄释放可能晚于 ffmpeg 返回。"""
        import shutil

        last_exc: OSError | None = None
        for i in range(attempts):
            try:
                await asyncio.to_thread(shutil.rmtree, path)
                return
            except OSError as exc:
                last_exc = exc
                await asyncio.sleep(0.15 * (i + 1))
        logger.warning(
            "临时目录清理失败 path=%s，怀疑句柄未释放；将依赖系统稍后回收。err=%s",
            path,
            last_exc,
        )

    async def finalize_local_video_after_summary(
        self, minute_token: str, *, owner_user_id: int
    ) -> None:
        """纪要完成后：上传分享片，再删除本地原片。"""
        if not self.enabled():
            return
        source = self._storage.find_video_path(
            minute_token, owner_user_id=owner_user_id
        )
        if source is None and not self.video_ready(
            minute_token, owner_user_id=owner_user_id
        ):
            return
        ok = await self.ensure_share_video(
            minute_token, owner_user_id=owner_user_id
        )
        if not ok:
            logger.warning(
                "纪要已完成但分享视频仍未上云 token=%s，本地原片暂不删除以便重试",
                minute_token,
            )
            return
        removed = self.purge_local_videos(
            minute_token, owner_user_id=owner_user_id
        )
        logger.info(
            "纪要完成后已清理本地原片 token=%s removed=%s",
            minute_token,
            removed,
        )

    def spawn_share_video(self, minute_token: str, *, owner_user_id: int) -> None:
        if not self.enabled():
            return
        owner = owner_user_id
        task: asyncio.Task[object] = asyncio.create_task(
            self.ensure_share_video(minute_token, owner_user_id=owner)
        )
        _r2_video_tasks.add(task)
        task.add_done_callback(_r2_video_tasks.discard)
        logger.info("已排队压缩并上传分享视频到 R2 token=%s", minute_token)

    def spawn_finalize_after_summary(
        self, minute_token: str, *, owner_user_id: int
    ) -> None:
        if not self.enabled():
            return
        owner = owner_user_id
        task: asyncio.Task[object] = asyncio.create_task(
            self.finalize_local_video_after_summary(
                minute_token, owner_user_id=owner
            )
        )
        _r2_video_tasks.add(task)
        task.add_done_callback(_r2_video_tasks.discard)
        logger.info(
            "已排队纪要完成后的分享上传与原片清理 token=%s", minute_token
        )

    async def sync_assets_safe(self, minute_token: str, *, owner_user_id: int) -> None:
        try:
            await self.sync_assets(minute_token, owner_user_id=owner_user_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "纪要配图同步 R2 失败 token=%s，本地文件暂保留: %s",
                minute_token,
                exc,
            )

    async def sync_summary_text(
        self,
        minute_token: str,
        content: str,
        *,
        owner_user_id: int,
    ) -> bool:
        """双写纪要正文到 R2；失败只打日志并返回 False，不抛。"""
        if not self.enabled():
            return False
        key = self.summary_key(minute_token, owner_user_id=owner_user_id)
        try:
            etag = await r2_client.upload_bytes(
                content.encode("utf-8"),
                key,
                content_type="text/markdown; charset=utf-8",
            )
            meta = await self.read_meta_async(
                minute_token, owner_user_id=owner_user_id
            )
            text_meta = dict(meta.get("text") or {})
            text_meta["summary"] = {
                "key": key,
                "etag": etag,
                "uploaded_at": utc_now_ms(),
            }
            meta["text"] = text_meta
            self.write_meta(minute_token, meta, owner_user_id=owner_user_id)
            logger.info("纪要正文已同步到 R2 token=%s key=%s", minute_token, key)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "纪要正文上传 R2 失败 token=%s，本地已落盘；"
                "公网读可能回落本地或变慢。err=%s",
                minute_token,
                exc,
            )
            return False

    async def sync_transcript_text(
        self,
        minute_token: str,
        content: str | bytes,
        *,
        owner_user_id: int,
        filename_suffix: str = "txt",
    ) -> bool:
        """双写转写文本到 R2；失败只打日志并返回 False，不抛。"""
        if not self.enabled():
            return False
        key = self.transcript_key(
            minute_token,
            owner_user_id=owner_user_id,
            filename_suffix=filename_suffix,
        )
        try:
            data = (
                content
                if isinstance(content, (bytes, bytearray))
                else str(content).encode("utf-8")
            )
            etag = await r2_client.upload_bytes(
                bytes(data),
                key,
                content_type="text/plain; charset=utf-8",
            )
            meta = await self.read_meta_async(
                minute_token, owner_user_id=owner_user_id
            )
            text_meta = dict(meta.get("text") or {})
            text_meta["transcript"] = {
                "key": key,
                "etag": etag,
                "uploaded_at": utc_now_ms(),
            }
            meta["text"] = text_meta
            self.write_meta(minute_token, meta, owner_user_id=owner_user_id)
            logger.info("转写文本已同步到 R2 token=%s key=%s", minute_token, key)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "转写文本上传 R2 失败 token=%s，本地已落盘；"
                "公网读可能回落本地或变慢。err=%s",
                minute_token,
                exc,
            )
            return False

    async def fetch_summary_text(
        self, minute_token: str, *, owner_user_id: int
    ) -> str | None:
        if not self.enabled():
            return None
        meta = await self.read_meta_async(
            minute_token, owner_user_id=owner_user_id
        )
        item = (meta.get("text") or {}).get("summary")
        if not isinstance(item, dict) or not item.get("key"):
            return None
        try:
            object_key = self._assert_owned_object_key(
                str(item["key"]),
                owner_user_id=owner_user_id,
                minute_token=minute_token,
            )
            data = await r2_client.get_bytes(object_key)
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning(
                    "R2 纪要正文非 UTF-8 token=%s，已按替换字符降级解码",
                    minute_token,
                )
                return data.decode("utf-8", errors="replace")
        except ValueError:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "从 R2 读取纪要正文失败 token=%s，将回落本地。err=%s",
                minute_token,
                exc,
            )
            return None

    async def fetch_transcript_text(
        self, minute_token: str, *, owner_user_id: int
    ) -> str | None:
        if not self.enabled():
            return None
        meta = await self.read_meta_async(
            minute_token, owner_user_id=owner_user_id
        )
        item = (meta.get("text") or {}).get("transcript")
        if not isinstance(item, dict) or not item.get("key"):
            return None
        try:
            object_key = self._assert_owned_object_key(
                str(item["key"]),
                owner_user_id=owner_user_id,
                minute_token=minute_token,
            )
            data = await r2_client.get_bytes(object_key)
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning(
                    "R2 转写文本非 UTF-8 token=%s，已按替换字符降级解码",
                    minute_token,
                )
                return data.decode("utf-8", errors="replace")
        except ValueError:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "从 R2 读取转写文本失败 token=%s，将回落本地。err=%s",
                minute_token,
                exc,
            )
            return None

    async def presign_summary_text(
        self, minute_token: str, *, owner_user_id: int
    ) -> str | None:
        if not self.enabled():
            return None
        meta = await self.read_meta_async(
            minute_token, owner_user_id=owner_user_id
        )
        item = (meta.get("text") or {}).get("summary")
        if not isinstance(item, dict) or not item.get("key"):
            return None
        try:
            object_key = self._assert_owned_object_key(
                str(item["key"]),
                owner_user_id=owner_user_id,
                minute_token=minute_token,
            )
            return await r2_client.presign_get(object_key)
        except ValueError:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "纪要正文签名 URL 生成失败 token=%s，将回落隧道传正文。err=%s",
                minute_token,
                exc,
            )
            return None

    async def presign_transcript_text(
        self, minute_token: str, *, owner_user_id: int
    ) -> str | None:
        if not self.enabled():
            return None
        meta = await self.read_meta_async(
            minute_token, owner_user_id=owner_user_id
        )
        item = (meta.get("text") or {}).get("transcript")
        if not isinstance(item, dict) or not item.get("key"):
            return None
        try:
            object_key = self._assert_owned_object_key(
                str(item["key"]),
                owner_user_id=owner_user_id,
                minute_token=minute_token,
            )
            return await r2_client.presign_get(object_key)
        except ValueError:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "转写正文签名 URL 生成失败 token=%s，将回落隧道传正文。err=%s",
                minute_token,
                exc,
            )
            return None

    async def sync_summary_text_safe(
        self,
        minute_token: str,
        content: str,
        *,
        owner_user_id: int,
    ) -> None:
        try:
            await self.sync_summary_text(
                minute_token, content, owner_user_id=owner_user_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "纪要正文同步 R2 异常 token=%s，本地已落盘；"
                "公网读可能回落本地或变慢。err=%s",
                minute_token,
                exc,
            )

    async def sync_transcript_text_safe(
        self,
        minute_token: str,
        content: str | bytes,
        *,
        owner_user_id: int,
        filename_suffix: str = "txt",
    ) -> None:
        try:
            await self.sync_transcript_text(
                minute_token,
                content,
                owner_user_id=owner_user_id,
                filename_suffix=filename_suffix,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "转写文本同步 R2 异常 token=%s，本地已落盘；"
                "公网读可能回落本地或变慢。err=%s",
                minute_token,
                exc,
            )


r2_media_service = R2MediaService()
