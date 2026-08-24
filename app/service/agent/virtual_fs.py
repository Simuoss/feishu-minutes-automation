"""虚拟目录：Agent 只看得见 /时间/会议名/转写.md 这样的路径。

真实磁盘路径、owner_user_id、minute_token 全都不出这个模块。Agent 手上只有
字符串路径，能解析出来的都是这次会话授权范围内的文件，解析不出来就是越权，
直接报错回去。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.service.meeting_storage_service import MeetingStorageService

logger = logging.getLogger(__name__)

# 目录名里的时间给人看，按北京时间显示，与前端各处一致
_LOCAL_TZ = timezone(timedelta(hours=8))

KIND_TRANSCRIPT = "transcript"
KIND_SUMMARY = "summary"

FILENAME_BY_KIND = {
    KIND_TRANSCRIPT: "转写.md",
    KIND_SUMMARY: "纪要.md",
}

# 会议名里的路径分隔符会把虚拟目录切乱，换掉
_PATH_UNSAFE_RE = re.compile(r"[/\\]+")
_UNKNOWN_TIME = "未知时间"


@dataclass(frozen=True)
class VirtualFile:
    path: str
    kind: str
    minute_token: str
    owner_user_id: int
    meeting_title: str
    meeting_time: str
    # 访客侧拼分享链接用；登录侧为 None
    share_token: str | None = None


@dataclass
class MeetingSource:
    """构树的输入：一场会议在本次会话里的身份。"""

    minute_token: str
    owner_user_id: int
    title: str
    create_time: str | None = None
    downloaded_at: int | None = None
    share_token: str | None = None


class VirtualFs:
    """一次会话的虚拟目录快照，附带正文缓存。"""

    def __init__(
        self,
        files: list[VirtualFile],
        *,
        storage: MeetingStorageService | None = None,
    ) -> None:
        self._files = {f.path: f for f in files}
        self._storage = storage or MeetingStorageService()
        self._lines_cache: dict[str, list[str]] = {}

    @property
    def files(self) -> list[VirtualFile]:
        return list(self._files.values())

    def __len__(self) -> int:
        return len(self._files)

    def resolve(self, path: str) -> VirtualFile | None:
        """只认树里真实存在的路径，顺手容忍一下模型常犯的写法。"""
        if not path:
            return None
        candidate = path.strip().replace("\\", "/")
        if not candidate.startswith("/"):
            candidate = "/" + candidate
        hit = self._files.get(candidate)
        if hit is not None:
            return hit
        # 模型偶尔把文件名写成「转写」或「转写.txt」，按目录+种类兜一次
        parent, _, tail = candidate.rpartition("/")
        for kind, filename in FILENAME_BY_KIND.items():
            stem = filename.rsplit(".", 1)[0]
            if tail in (stem, f"{stem}.txt", f"{stem}.md", kind):
                retry = self._files.get(f"{parent}/{filename}")
                if retry is not None:
                    return retry
        return None

    def tree_listing(self) -> str:
        """给系统提示词用的目录清单，让 Agent 一开始就知道有哪些资料。"""
        if not self._files:
            return "（这个范围内还没有任何可检索的资料）"
        by_dir: dict[str, list[str]] = {}
        for vf in self._files.values():
            parent, _, filename = vf.path.rpartition("/")
            by_dir.setdefault(parent, []).append(filename)
        lines: list[str] = []
        for parent in sorted(by_dir):
            lines.append(f"{parent}/ -> {'、'.join(sorted(by_dir[parent]))}")
        return "\n".join(lines)

    async def lines(self, vf: VirtualFile) -> list[str]:
        """按行取正文。同一会话内只读盘一次，保证三个工具看到的行号一致。"""
        cached = self._lines_cache.get(vf.path)
        if cached is not None:
            return cached
        text = await self._load_text(vf)
        lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self._lines_cache[vf.path] = lines
        return lines

    async def _load_text(self, vf: VirtualFile) -> str:
        if vf.kind == KIND_TRANSCRIPT:
            text = await self._storage.read_transcript_async(
                vf.minute_token, owner_user_id=vf.owner_user_id
            )
            return text or ""
        payload = await self._storage.read_summary_async(
            vf.minute_token, owner_user_id=vf.owner_user_id
        )
        content = str((payload or {}).get("content") or "")
        if not content:
            return ""
        from app.service.speaker_naming_service import apply_speaker_names_to_summary

        return await apply_speaker_names_to_summary(
            content, vf.minute_token, owner_user_id=vf.owner_user_id
        )


def _sanitize_title(title: str | None) -> str:
    cleaned = _PATH_UNSAFE_RE.sub("_", (title or "").strip())
    cleaned = cleaned.strip(". ")
    if not cleaned:
        return "未命名会议"
    return cleaned[:60]


def _from_epoch(value: int) -> datetime:
    seconds = value / 1000.0 if value > 10_000_000_000 else float(value)
    return datetime.fromtimestamp(seconds, tz=_LOCAL_TZ).replace(tzinfo=None)


def _parse_display_time(raw: str | None) -> datetime | None:
    """解析成给人看的挂钟时间。

    飞书的 create_time 在库里是毫秒时间戳字符串，共享的 parse_create_time 只认
    ISO，所以这里自己认一遍。没带时区的字面时间按原样显示，不凭空平移。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        return _from_epoch(int(text))
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(_LOCAL_TZ).replace(tzinfo=None)


def _time_label(source: MeetingSource) -> str:
    parsed = _parse_display_time(source.create_time)
    if parsed is None and source.downloaded_at:
        parsed = _from_epoch(source.downloaded_at)
    if parsed is None:
        return _UNKNOWN_TIME
    return parsed.strftime("%Y-%m-%d %H%M")


def _dir_for(source: MeetingSource, used: set[str]) -> str:
    base = f"/{_time_label(source)}/{_sanitize_title(source.title)}"
    if base not in used:
        used.add(base)
        return base
    # 同一分钟撞上同名会议，加序号区分，别让两场课挤进一个目录
    for n in range(2, 100):
        candidate = f"{base} ({n})"
        if candidate not in used:
            used.add(candidate)
            return candidate
    used.add(base)
    return base


@dataclass
class _Availability:
    has_transcript: bool = False
    has_summary: bool = False


def _probe(
    storage: MeetingStorageService, sources: list[MeetingSource]
) -> list[_Availability]:
    result: list[_Availability] = []
    for source in sources:
        try:
            result.append(
                _Availability(
                    has_transcript=storage.has_transcript(
                        source.minute_token, owner_user_id=source.owner_user_id
                    ),
                    has_summary=storage.has_summary(
                        source.minute_token, owner_user_id=source.owner_user_id
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "探测会议资料是否存在失败 token=%s：%s，本场按无资料处理",
                source.minute_token,
                exc,
            )
            result.append(_Availability())
    return result


async def build_virtual_fs(
    sources: list[MeetingSource],
    *,
    storage: MeetingStorageService | None = None,
) -> VirtualFs:
    """按会议清单构树；只收录盘上真有正文的那些。"""
    store = storage or MeetingStorageService()
    availability = await asyncio.to_thread(_probe, store, sources)

    used_dirs: set[str] = set()
    files: list[VirtualFile] = []
    for source, avail in zip(sources, availability, strict=True):
        if not (avail.has_transcript or avail.has_summary):
            continue
        directory = _dir_for(source, used_dirs)
        time_label = _time_label(source)
        title = _sanitize_title(source.title)
        kinds = []
        if avail.has_transcript:
            kinds.append(KIND_TRANSCRIPT)
        if avail.has_summary:
            kinds.append(KIND_SUMMARY)
        for kind in kinds:
            files.append(
                VirtualFile(
                    path=f"{directory}/{FILENAME_BY_KIND[kind]}",
                    kind=kind,
                    minute_token=source.minute_token,
                    owner_user_id=source.owner_user_id,
                    meeting_title=title,
                    meeting_time=time_label,
                    share_token=source.share_token,
                )
            )
    return VirtualFs(files, storage=store)


async def collect_owner_sources(owner_user_id: int | None) -> list[MeetingSource]:
    """登录用户/超管：从库里取会议清单。"""
    from app.repository.uow import UnitOfWork

    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        records = await uow.meeting_records.list_latest_by_scope(
            owner_user_id=owner_user_id, limit=2000
        )

    sources: list[MeetingSource] = []
    for record in records:
        if not record.minute_token or record.owner_user_id is None:
            continue
        sources.append(
            MeetingSource(
                minute_token=record.minute_token,
                owner_user_id=int(record.owner_user_id),
                title=record.title or "",
                create_time=record.create_time,
                downloaded_at=record.downloaded_at,
            )
        )
    sources.sort(key=lambda s: _time_label(s), reverse=True)
    return sources
