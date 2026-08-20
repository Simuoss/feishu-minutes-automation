import asyncio
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from app.core.config import settings
from app.core.resource_key import require_owner_user_id, storage_relpath
from app.integrations.feishu.client import FeishuApiClient

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".ogg"}

# 自建转写的文件名；与飞书原文的 transcript.txt 并存，读取时优先它
ASR_TRANSCRIPT_FILENAME = "transcript.asr.txt"


class MeetingStorageService:
    """本地会议资源存储：路径为 {storage_root}/{owner_user_id}/{minute_token}/。"""

    def __init__(self, storage_root: str | None = None) -> None:
        self._root = Path(storage_root or settings.storage_root)

    def get_meeting_dir(self, minute_token: str, *, owner_user_id: int) -> Path:
        rel = storage_relpath(owner_user_id, minute_token)
        return self._root.joinpath(*rel.split("/"))

    def ensure_layout(
        self, minute_token: str, *, owner_user_id: int
    ) -> dict[str, Path]:
        base = self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
        layout = {
            "base": base,
            "raw": base / "raw",
            "media": base / "raw" / "media",
            "transcript": base / "raw" / "transcript",
            "agent": base / "agent",
            "llm": base / "llm",
            "output": base / "output",
        }
        for path in layout.values():
            path.mkdir(parents=True, exist_ok=True)
        return layout

    @staticmethod
    def _guess_extension(content_type: str | None, url: str) -> str:
        if content_type:
            ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
            if ext:
                return ext
        url_path = url.split("?")[0]
        suffix = Path(url_path).suffix
        return suffix if suffix else ".bin"

    @staticmethod
    def _detect_has_video(filename: str, content_type: str | None) -> bool:
        suffix = Path(filename).suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            return True
        if suffix in AUDIO_EXTENSIONS:
            return False
        if content_type:
            main_type = content_type.split(";")[0].strip().lower()
            if main_type.startswith("video/"):
                return True
            if main_type.startswith("audio/"):
                return False
        return False

    @staticmethod
    def _normalize_media_filename(filename: str) -> str:
        cleaned = filename.strip().strip('"')
        if "%" in cleaned:
            decoded = unquote(cleaned)
            if decoded:
                return decoded
        return cleaned

    async def save_media(
        self,
        minute_token: str,
        download_url: str,
        api: FeishuApiClient,
        *,
        owner_user_id: int,
    ) -> tuple[Path, bool]:
        layout = self.ensure_layout(minute_token, owner_user_id=owner_user_id)
        response = await api.download_url(download_url)
        content_type = response.headers.get("content-type")
        ext = self._guess_extension(content_type, download_url)

        disposition = response.headers.get("content-disposition", "")
        filename_match = re.search(r'filename="?([^";]+)"?', disposition)
        raw_filename = filename_match.group(1) if filename_match else f"media{ext}"
        filename = self._normalize_media_filename(raw_filename)
        if not Path(filename).suffix:
            filename = f"{filename}{ext}"

        media_path = layout["media"] / filename
        media_path.write_bytes(response.content)

        has_video = self._detect_has_video(filename, content_type)
        logger.info(
            "妙记媒体已保存 owner=%s token=%s path=%s has_video=%s",
            owner_user_id,
            minute_token,
            media_path,
            has_video,
        )
        return media_path, has_video

    def save_transcript(
        self,
        minute_token: str,
        content: bytes,
        *,
        owner_user_id: int,
        file_format: str = "txt",
    ) -> Path:
        layout = self.ensure_layout(minute_token, owner_user_id=owner_user_id)
        transcript_path = layout["transcript"] / f"transcript.{file_format}"
        transcript_path.write_bytes(content)
        logger.info(
            "妙记转写已保存 owner=%s token=%s path=%s",
            owner_user_id,
            minute_token,
            transcript_path,
        )
        return transcript_path

    def _list_transcript_files(self, minute_token: str, *, owner_user_id: int) -> list[Path]:
        """按优先级列出转写文件：自建转写覆盖全程，排在飞书原文前面。"""
        transcript_dir = (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "raw"
            / "transcript"
        )
        if not transcript_dir.is_dir():
            return []
        asr: list[Path] = []
        feishu: list[Path] = []
        for f in sorted(transcript_dir.iterdir()):
            if not (f.is_file() and f.suffix.lower() in {".txt", ".srt"}):
                continue
            (asr if f.name == ASR_TRANSCRIPT_FILENAME else feishu).append(f)
        return asr + feishu

    def _read_transcript_local(
        self, minute_token: str, *, owner_user_id: int
    ) -> str | None:
        for f in self._list_transcript_files(minute_token, owner_user_id=owner_user_id):
            try:
                return f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                logger.warning(
                    "转写文件非 UTF-8 编码 path=%s，怀疑下载时被改写，已按替换字符降级读取",
                    f,
                )
                return f.read_text(encoding="utf-8", errors="replace")
        return None

    def read_feishu_transcript(
        self, minute_token: str, *, owner_user_id: int
    ) -> str | None:
        """只读飞书原文。

        默认读路径是 ASR 优先，但飞书那份带的是真名，声纹提炼要的正是它。
        """
        for f in self._list_transcript_files(
            minute_token, owner_user_id=owner_user_id
        ):
            if f.name == ASR_TRANSCRIPT_FILENAME:
                continue
            return f.read_text(encoding="utf-8", errors="replace")
        return None

    def read_transcript(
        self, minute_token: str, *, owner_user_id: int
    ) -> str | None:
        """同步门面：供 worker/导出使用；async 路由请用 read_transcript_async。"""
        from app.core.async_bridge import run_async

        return run_async(
            self.read_transcript_async(
                minute_token, owner_user_id=owner_user_id
            )
        )

    async def read_transcript_async(
        self, minute_token: str, *, owner_user_id: int, apply_names: bool = True
    ) -> str | None:
        """读转写。默认贴上真名；写纪要时要原始编号，传 apply_names=False。"""
        text = await asyncio.to_thread(
            self._read_transcript_local,
            minute_token,
            owner_user_id=owner_user_id,
        )
        if text is None or not apply_names:
            return text
        from app.service.speaker_naming_service import apply_speaker_names

        # 盘上写的是「说话人N」，真名在这里按声纹库贴上去，改名才能立刻全站生效
        return await apply_speaker_names(
            text, minute_token, owner_user_id=owner_user_id
        )

    def get_summary_path(
        self, minute_token: str, *, owner_user_id: int
    ) -> Path:
        return (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "output"
            / "summary.md"
        )

    def get_assets_dir(self, minute_token: str, *, owner_user_id: int) -> Path:
        return (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "output"
            / "assets"
        )

    def ensure_assets_dir(self, minute_token: str, *, owner_user_id: int) -> Path:
        assets = self.get_assets_dir(minute_token, owner_user_id=owner_user_id)
        assets.mkdir(parents=True, exist_ok=True)
        return assets

    def clear_assets(self, minute_token: str, *, owner_user_id: int) -> int:
        assets = self.get_assets_dir(minute_token, owner_user_id=owner_user_id)
        if not assets.is_dir():
            return 0
        removed = 0
        for f in assets.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
                removed += 1
        return removed

    def get_frames_work_dir(
        self, minute_token: str, *, owner_user_id: int
    ) -> Path:
        return (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "agent"
            / "frames"
        )

    def get_figures_manifest_path(
        self, minute_token: str, *, owner_user_id: int
    ) -> Path:
        return (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "agent"
            / "figures.manifest.json"
        )

    def get_redaction_dir(
        self, minute_token: str, *, owner_user_id: int
    ) -> Path:
        return (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "agent"
            / "redaction"
        )

    def get_redaction_originals_dir(
        self, minute_token: str, *, owner_user_id: int
    ) -> Path:
        path = self.get_redaction_dir(minute_token, owner_user_id=owner_user_id) / "originals"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_redaction_audit_path(
        self, minute_token: str, *, owner_user_id: int
    ) -> Path:
        return (
            self.get_redaction_dir(minute_token, owner_user_id=owner_user_id)
            / "audit.json"
        )

    def resolve_agent_relative_path(
        self, minute_token: str, relative: str, *, owner_user_id: int
    ) -> Path | None:
        cleaned = unquote((relative or "").strip().lstrip("/").replace("\\", "/"))
        if not cleaned or ".." in cleaned.split("/"):
            return None
        base = self.get_meeting_dir(
            minute_token, owner_user_id=owner_user_id
        ).resolve()
        candidate = (base / cleaned).resolve()
        if not candidate.is_file():
            return None
        if base != candidate and base not in candidate.parents:
            return None
        return candidate

    def resolve_asset_path(
        self, minute_token: str, filename: str, *, owner_user_id: int
    ) -> Path | None:
        assets = self.get_assets_dir(minute_token, owner_user_id=owner_user_id)
        if not assets.is_dir():
            return None
        candidate = (assets / unquote(filename)).resolve()
        if not candidate.is_file():
            return None
        if assets.resolve() not in candidate.parents:
            return None
        return candidate

    def find_video_path(
        self, minute_token: str, *, owner_user_id: int
    ) -> Path | None:
        media_dir = (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "raw"
            / "media"
        )
        if not media_dir.is_dir():
            return None
        for f in sorted(media_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                return f
        return None

    def find_media_path(
        self, minute_token: str, *, owner_user_id: int
    ) -> Path | None:
        """找出这场会议的媒体文件，音频视频都算（转写只需要声音）。"""
        media_dir = (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "raw"
            / "media"
        )
        if not media_dir.is_dir():
            return None
        for f in sorted(media_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
                return f
        return None

    def get_asr_work_dir(self, minute_token: str, *, owner_user_id: int) -> Path:
        path = (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "agent"
            / "asr"
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_asr_transcript(
        self, minute_token: str, content: str, *, owner_user_id: int
    ) -> Path:
        """自建转写单独存一个文件，飞书原文原样保留。"""
        layout = self.ensure_layout(minute_token, owner_user_id=owner_user_id)
        path = layout["transcript"] / ASR_TRANSCRIPT_FILENAME
        path.write_text(content, encoding="utf-8")
        logger.info(
            "自建转写已保存 owner=%s token=%s path=%s",
            owner_user_id,
            minute_token,
            path,
        )
        return path

    def has_summary(self, minute_token: str, *, owner_user_id: int) -> bool:
        return self.get_summary_path(
            minute_token, owner_user_id=owner_user_id
        ).is_file()

    async def save_summary_async(
        self,
        minute_token: str,
        content: str,
        meta: dict[str, Any],
        *,
        owner_user_id: int,
    ) -> Path:
        layout = self.ensure_layout(minute_token, owner_user_id=owner_user_id)
        summary_path = layout["output"] / "summary.md"
        await asyncio.to_thread(summary_path.write_text, content, "utf-8")
        from app.service.metadata_db_service import upsert_summary_meta

        try:
            await upsert_summary_meta(
                minute_token, meta, owner_user_id=owner_user_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "纪要正文已落盘但元数据写 DB 失败 token=%s owner=%s，指标接口可能缺数据。err=%s",
                minute_token,
                owner_user_id,
                exc,
            )
            raise
        logger.info(
            "会议纪要已保存 owner=%s token=%s path=%s",
            owner_user_id,
            minute_token,
            summary_path,
        )
        return summary_path

    def save_summary(
        self,
        minute_token: str,
        content: str,
        meta: dict[str, Any],
        *,
        owner_user_id: int,
    ) -> Path:
        from app.core.async_bridge import run_async

        return run_async(
            self.save_summary_async(
                minute_token, content, meta, owner_user_id=owner_user_id
            )
        )

    async def read_summary_async(
        self, minute_token: str, *, owner_user_id: int
    ) -> dict[str, Any] | None:
        from app.service.metadata_db_service import read_summary_meta

        async def _load_db_meta() -> dict[str, Any]:
            try:
                db_meta = await read_summary_meta(
                    minute_token, owner_user_id=owner_user_id
                )
                return db_meta or {}
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "从 DB 读取纪要元数据失败 token=%s，将只返回正文。err=%s",
                    minute_token,
                    exc,
                )
                return {}

        summary_path = self.get_summary_path(
            minute_token, owner_user_id=owner_user_id
        )
        if not await asyncio.to_thread(summary_path.is_file):
            return None

        try:
            content = await asyncio.to_thread(
                summary_path.read_text, encoding="utf-8"
            )
        except UnicodeDecodeError:
            content = await asyncio.to_thread(
                summary_path.read_text, encoding="utf-8", errors="replace"
            )

        return {
            "minute_token": minute_token,
            "content": content,
            "meta": await _load_db_meta(),
        }

    def read_summary(
        self, minute_token: str, *, owner_user_id: int
    ) -> dict[str, Any] | None:
        """同步门面：供 worker 使用；async 路由请用 read_summary_async。"""
        from app.core.async_bridge import run_async

        return run_async(
            self.read_summary_async(minute_token, owner_user_id=owner_user_id)
        )

    def has_media(self, minute_token: str, *, owner_user_id: int) -> bool:
        media_dir = (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "raw"
            / "media"
        )
        return media_dir.is_dir() and any(media_dir.iterdir())

    def has_transcript(self, minute_token: str, *, owner_user_id: int) -> bool:
        transcript_dir = (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "raw"
            / "transcript"
        )
        return transcript_dir.is_dir() and any(transcript_dir.iterdir())

    def clear_media(self, minute_token: str, *, owner_user_id: int) -> None:
        media_dir = (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "raw"
            / "media"
        )
        if not media_dir.is_dir():
            return
        for path in media_dir.iterdir():
            if path.is_file():
                path.unlink()

    def clear_transcript(self, minute_token: str, *, owner_user_id: int) -> None:
        transcript_dir = (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "raw"
            / "transcript"
        )
        if not transcript_dir.is_dir():
            return
        for path in transcript_dir.iterdir():
            if path.is_file():
                path.unlink()

    def _list_media_files(self, base: Path) -> list[dict[str, str]]:
        media_files: list[dict[str, str]] = []
        media_dir = base / "raw" / "media"
        if media_dir.is_dir():
            for f in sorted(media_dir.iterdir()):
                if f.is_file():
                    media_files.append(
                        {"name": f.name, "kind": self._media_kind(f.name)}
                    )
        return media_files

    async def get_local_detail_async(
        self, minute_token: str, *, owner_user_id: int
    ) -> dict[str, Any] | None:
        from app.service.metadata_db_service import ms_to_iso, read_meeting_record

        base = self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
        record = await read_meeting_record(
            minute_token, owner_user_id=owner_user_id
        )
        media_files = await asyncio.to_thread(self._list_media_files, base)
        has_transcript = await asyncio.to_thread(
            self.has_transcript, minute_token, owner_user_id=owner_user_id
        )

        if record is None and not media_files and not has_transcript:
            return None

        return {
            "minute_token": minute_token,
            "title": record.title if record else None,
            "duration_ms": record.duration_ms if record else None,
            "has_video": record.has_video if record else None,
            "media_files": media_files,
            "has_transcript": has_transcript,
            "downloaded_at": ms_to_iso(record.downloaded_at) if record else None,
        }

    def get_local_detail(
        self, minute_token: str, *, owner_user_id: int
    ) -> dict[str, Any] | None:
        """同步门面：供 worker 使用；async 路由请用 get_local_detail_async。"""
        from app.core.async_bridge import run_async

        return run_async(
            self.get_local_detail_async(
                minute_token, owner_user_id=owner_user_id
            )
        )

    @staticmethod
    def _media_kind(filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            return "video"
        if suffix in AUDIO_EXTENSIONS:
            return "audio"
        return "unknown"

    def resolve_media_path(
        self, minute_token: str, filename: str, *, owner_user_id: int
    ) -> Path | None:
        media_dir = (
            self.get_meeting_dir(minute_token, owner_user_id=owner_user_id)
            / "raw"
            / "media"
        )
        if not media_dir.is_dir():
            return None

        candidates = [filename]
        decoded = unquote(filename)
        if decoded not in candidates:
            candidates.append(decoded)
        stripped = (decoded or filename).strip()
        if stripped and stripped not in candidates:
            candidates.append(stripped)
        encoded = self._normalize_media_filename(filename)
        if encoded not in candidates:
            candidates.append(encoded)
        url_encoded = quote(decoded or filename, safe="")
        if url_encoded not in candidates:
            candidates.append(url_encoded)
        if stripped:
            stripped_encoded = quote(stripped, safe="")
            if stripped_encoded not in candidates:
                candidates.append(stripped_encoded)

        for name in candidates:
            candidate = (media_dir / name).resolve()
            if not candidate.is_file():
                continue
            if media_dir.resolve() not in candidate.parents:
                continue
            return candidate
        return None
