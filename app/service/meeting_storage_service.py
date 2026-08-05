import logging
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from app.core.config import settings
from app.integrations.feishu.client import FeishuApiClient

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".ogg"}


class MeetingStorageService:
    """本地会议资源存储，预留后续 AI / Agent / 云文档目录。"""

    def __init__(self, storage_root: str | None = None) -> None:
        self._root = Path(storage_root or settings.storage_root)

    def get_meeting_dir(self, minute_token: str) -> Path:
        return self._root / minute_token

    def ensure_layout(self, minute_token: str) -> dict[str, Path]:
        base = self.get_meeting_dir(minute_token)
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

    def write_meta(self, minute_token: str, meta: dict[str, Any]) -> Path:
        """会议元数据只写 DB；仍确保目录布局以便落媒体/转写。"""
        layout = self.ensure_layout(minute_token)
        from app.core.async_bridge import run_async
        from app.service.metadata_db_service import upsert_meeting_meta

        try:
            run_async(upsert_meeting_meta(minute_token, meta))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "写入会议元数据到 DB 失败 token=%s，后续列表/分享可能缺标题。err=%s",
                minute_token,
                exc,
            )
            raise
        return layout["base"]

    def read_meta(self, minute_token: str) -> dict[str, Any] | None:
        from app.core.async_bridge import run_async
        from app.service.metadata_db_service import read_meeting_meta

        try:
            return run_async(read_meeting_meta(minute_token))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "从 DB 读取会议元数据失败 token=%s，将按空元数据继续。err=%s",
                minute_token,
                exc,
            )
            return None

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
    ) -> tuple[Path, bool]:
        layout = self.ensure_layout(minute_token)
        response = await api.download_url(download_url)
        content_type = response.headers.get("content-type")
        ext = self._guess_extension(content_type, download_url)

        # 尝试从 Content-Disposition 获取文件名
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
            "妙记媒体已保存 token=%s path=%s has_video=%s",
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
        file_format: str = "txt",
    ) -> Path:
        layout = self.ensure_layout(minute_token)
        transcript_path = layout["transcript"] / f"transcript.{file_format}"
        transcript_path.write_bytes(content)
        logger.info("妙记转写已保存 token=%s path=%s", minute_token, transcript_path)
        return transcript_path

    def read_transcript(self, minute_token: str) -> str | None:
        transcript_dir = self.get_meeting_dir(minute_token) / "raw" / "transcript"
        if not transcript_dir.is_dir():
            return None
        for f in sorted(transcript_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in {".txt", ".srt"}:
                try:
                    return f.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    logger.warning(
                        "转写文件非 UTF-8 编码 path=%s，怀疑下载时被改写，已按替换字符降级读取",
                        f,
                    )
                    return f.read_text(encoding="utf-8", errors="replace")
        return None

    def get_summary_path(self, minute_token: str) -> Path:
        return self.get_meeting_dir(minute_token) / "output" / "summary.md"

    def get_assets_dir(self, minute_token: str) -> Path:
        return self.get_meeting_dir(minute_token) / "output" / "assets"

    def ensure_assets_dir(self, minute_token: str) -> Path:
        assets = self.get_assets_dir(minute_token)
        assets.mkdir(parents=True, exist_ok=True)
        return assets

    def clear_assets(self, minute_token: str) -> int:
        """清空配图目录，重新生成前调用，避免上一版的图残留成孤儿文件。"""
        assets = self.get_assets_dir(minute_token)
        if not assets.is_dir():
            return 0
        removed = 0
        for f in assets.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
                removed += 1
        return removed

    def get_frames_work_dir(self, minute_token: str) -> Path:
        """抽帧的中间目录，放在 agent/ 下，不随 output/ 对外暴露。"""
        return self.get_meeting_dir(minute_token) / "agent" / "frames"

    def get_figures_manifest_path(self, minute_token: str) -> Path:
        return self.get_meeting_dir(minute_token) / "agent" / "figures.manifest.json"

    def get_redaction_dir(self, minute_token: str) -> Path:
        return self.get_meeting_dir(minute_token) / "agent" / "redaction"

    def get_redaction_originals_dir(self, minute_token: str) -> Path:
        path = self.get_redaction_dir(minute_token) / "originals"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_redaction_audit_path(self, minute_token: str) -> Path:
        return self.get_redaction_dir(minute_token) / "audit.json"

    def resolve_agent_relative_path(
        self, minute_token: str, relative: str
    ) -> Path | None:
        """解析会议目录下的相对路径（如 agent/redaction/originals/fig-01.jpg）。"""
        cleaned = unquote((relative or "").strip().lstrip("/").replace("\\", "/"))
        if not cleaned or ".." in cleaned.split("/"):
            return None
        base = self.get_meeting_dir(minute_token).resolve()
        candidate = (base / cleaned).resolve()
        if not candidate.is_file():
            return None
        if base != candidate and base not in candidate.parents:
            return None
        return candidate

    def resolve_asset_path(self, minute_token: str, filename: str) -> Path | None:
        assets = self.get_assets_dir(minute_token)
        if not assets.is_dir():
            return None
        candidate = (assets / unquote(filename)).resolve()
        if not candidate.is_file():
            return None
        # 防止 ../ 穿越到配图目录之外
        if assets.resolve() not in candidate.parents:
            return None
        return candidate

    def find_video_path(self, minute_token: str) -> Path | None:
        media_dir = self.get_meeting_dir(minute_token) / "raw" / "media"
        if not media_dir.is_dir():
            return None
        for f in sorted(media_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                return f
        return None

    def has_summary(self, minute_token: str) -> bool:
        return self.get_summary_path(minute_token).is_file()

    def save_summary(
        self,
        minute_token: str,
        content: str,
        meta: dict[str, Any],
    ) -> Path:
        layout = self.ensure_layout(minute_token)
        summary_path = layout["output"] / "summary.md"
        summary_path.write_text(content, encoding="utf-8")
        from app.core.async_bridge import run_async
        from app.service.metadata_db_service import upsert_summary_meta

        try:
            run_async(upsert_summary_meta(minute_token, meta))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "纪要正文已落盘但元数据写 DB 失败 token=%s，指标接口可能缺数据。err=%s",
                minute_token,
                exc,
            )
            raise
        logger.info("会议纪要已保存 token=%s path=%s", minute_token, summary_path)
        return summary_path

    def read_summary(self, minute_token: str) -> dict[str, Any] | None:
        summary_path = self.get_summary_path(minute_token)
        if not summary_path.is_file():
            return None

        from app.core.async_bridge import run_async
        from app.service.metadata_db_service import read_summary_meta

        meta: dict[str, Any] = {}
        try:
            db_meta = run_async(read_summary_meta(minute_token))
            if db_meta:
                meta = db_meta
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "从 DB 读取纪要元数据失败 token=%s，将只返回正文。err=%s",
                minute_token,
                exc,
            )

        return {
            "minute_token": minute_token,
            "content": summary_path.read_text(encoding="utf-8"),
            "meta": meta,
        }

    def has_media(self, minute_token: str) -> bool:
        media_dir = self.get_meeting_dir(minute_token) / "raw" / "media"
        return media_dir.is_dir() and any(media_dir.iterdir())

    def has_transcript(self, minute_token: str) -> bool:
        transcript_dir = self.get_meeting_dir(minute_token) / "raw" / "transcript"
        return transcript_dir.is_dir() and any(transcript_dir.iterdir())

    def clear_media(self, minute_token: str) -> None:
        media_dir = self.get_meeting_dir(minute_token) / "raw" / "media"
        if not media_dir.is_dir():
            return
        for path in media_dir.iterdir():
            if path.is_file():
                path.unlink()

    def clear_transcript(self, minute_token: str) -> None:
        transcript_dir = self.get_meeting_dir(minute_token) / "raw" / "transcript"
        if not transcript_dir.is_dir():
            return
        for path in transcript_dir.iterdir():
            if path.is_file():
                path.unlink()

    def is_persisted(self, minute_token: str) -> bool:
        if self.has_media(minute_token) or self.has_transcript(minute_token):
            return True
        meta = self.read_meta(minute_token)
        return bool(meta and meta.get("status") == "COMPLETED")

    def get_local_detail(self, minute_token: str) -> dict[str, Any] | None:
        base = self.get_meeting_dir(minute_token)
        meta = self.read_meta(minute_token) or {}

        media_files: list[dict[str, str]] = []
        media_dir = base / "raw" / "media"
        if media_dir.is_dir():
            for f in sorted(media_dir.iterdir()):
                if f.is_file():
                    media_files.append({"name": f.name, "kind": self._media_kind(f.name)})

        transcript_text = self.read_transcript(minute_token)

        if not meta and not media_files and transcript_text is None:
            return None

        return {
            "minute_token": minute_token,
            "title": meta.get("title"),
            "duration_ms": meta.get("duration_ms"),
            "has_video": meta.get("has_video"),
            "media_files": media_files,
            "transcript": transcript_text,
            "meta": meta,
        }

    @staticmethod
    def _media_kind(filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            return "video"
        if suffix in AUDIO_EXTENSIONS:
            return "audio"
        return "unknown"

    def resolve_media_path(self, minute_token: str, filename: str) -> Path | None:
        media_dir = self.get_meeting_dir(minute_token) / "raw" / "media"
        if not media_dir.is_dir():
            return None

        candidates = [filename]
        decoded = unquote(filename)
        if decoded not in candidates:
            candidates.append(decoded)
        # 飞书落盘偶发文件名带首尾空白（URL 里会变成 %20…），一并尝试
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
