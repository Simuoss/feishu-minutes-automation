"""检索助手的三个工具：search / read / send。

工具是按会话现造的闭包，直接抓住这次会话的虚拟目录，所以不存在拿错范围的可能。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import tool

from app.service.agent.link_builder import build_link
from app.service.agent.virtual_fs import VirtualFile, VirtualFs

logger = logging.getLogger(__name__)

MODE_KEYWORD = "keyword"
MODE_REGEX = "regex"
LOGIC_OR = "or"
LOGIC_AND = "and"

CONTEXT_LINES = 3
MAX_PATTERNS = 8
MAX_LINE_CHARS = 220
READ_DEFAULT_LINES = 200
READ_MAX_LINES = 2000
READ_DEFAULT_LIMIT = 5000
READ_MAX_LIMIT = 20000
SEND_MAX_LINES = 400
SEND_QUOTE_MAX_CHARS = 2000


@dataclass
class CardPayload:
    """send 工具推给用户的引文卡片。"""

    path: str
    meeting_title: str
    meeting_time: str
    start_line: int
    end_line: int
    quote: str
    url: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "meeting_title": self.meeting_title,
            "meeting_time": self.meeting_time,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "quote": self.quote,
            "url": self.url,
            "note": self.note,
        }


@dataclass
class _Hit:
    vf: VirtualFile
    line_no: int
    matched: str


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        payload["is_error"] = True
    return payload


def _clip(line: str) -> str:
    stripped = line.rstrip()
    if len(stripped) <= MAX_LINE_CHARS:
        return stripped
    return stripped[:MAX_LINE_CHARS] + "…"


def _numbered(lines: list[str], line_no: int, *, marker: bool = False) -> str:
    prefix = ">" if marker else " "
    return f"{prefix} {line_no}| {_clip(lines[line_no - 1])}"


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_patterns(raw: Any) -> list[str]:
    if isinstance(raw, str):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = [str(item) for item in raw]
    else:
        candidates = []
    cleaned = [item.strip() for item in candidates if str(item).strip()]
    return cleaned[:MAX_PATTERNS]


@dataclass
class AgentToolbox:
    """把虚拟目录、卡片回调和事件回调打包成一组可以喂给 SDK 的工具。"""

    vfs: VirtualFs
    scope: str
    max_results: int = 50
    on_card: Callable[[CardPayload], Awaitable[None]] | None = None
    on_tool_start: Callable[[str, str], Awaitable[None]] | None = None
    on_tool_end: Callable[[str, str], Awaitable[None]] | None = None
    cards: list[CardPayload] = field(default_factory=list)

    async def _started(self, name: str, brief: str) -> None:
        if self.on_tool_start is not None:
            await self.on_tool_start(name, brief)

    async def _ended(self, name: str, summary: str) -> None:
        if self.on_tool_end is not None:
            await self.on_tool_end(name, summary)

    # ---------- search ----------

    async def search(self, args: dict[str, Any]) -> dict[str, Any]:
        patterns = _normalize_patterns(args.get("patterns"))
        mode = str(args.get("mode") or MODE_KEYWORD).strip().lower()
        logic = str(args.get("logic") or LOGIC_OR).strip().lower()
        limit = max(1, min(_as_int(args.get("max_results"), self.max_results), 200))
        scope_paths = _normalize_paths(args.get("paths"))

        brief = f"检索 {'、'.join(patterns) or '（空）'}｜{'正则' if mode == MODE_REGEX else '关键字'}｜{'与' if logic == LOGIC_AND else '或'}"
        await self._started("search", brief)

        if not patterns:
            await self._ended("search", "没给检索词")
            return _text_result("patterns 不能为空，至少给一个检索词。", is_error=True)

        try:
            matchers = _build_matchers(patterns, mode)
        except re.error as exc:
            await self._ended("search", "正则写错了")
            return _text_result(f"正则表达式不合法：{exc}", is_error=True)

        targets = self._targets(scope_paths)
        if not targets:
            await self._ended("search", "范围内没有文件")
            return _text_result(
                "指定的 paths 在可检索范围内不存在，去掉 paths 或改用目录清单里的路径。",
                is_error=True,
            )

        hits: list[_Hit] = []
        files_hit = 0
        for vf in targets:
            lines = await self.vfs.lines(vf)
            per_pattern: list[list[_Hit]] = []
            for label, matcher in matchers:
                found = [
                    _Hit(vf=vf, line_no=idx + 1, matched=label)
                    for idx, line in enumerate(lines)
                    if matcher(line)
                ]
                per_pattern.append(found)
            if logic == LOGIC_AND and any(not found for found in per_pattern):
                continue
            merged = sorted(
                (hit for found in per_pattern for hit in found),
                key=lambda h: h.line_no,
            )
            if merged:
                files_hit += 1
                hits.extend(merged)

        total = len(hits)
        truncated = total > limit
        shown = hits[:limit]
        text = await self._render_hits(
            shown,
            total=total,
            files_hit=files_hit,
            scanned=len(targets),
            truncated=truncated,
        )
        await self._ended(
            "search",
            f"命中 {total} 处，分布在 {files_hit} 个文件" if total else "没有命中",
        )
        return _text_result(text)

    def _targets(self, scope_paths: list[str]) -> list[VirtualFile]:
        if not scope_paths:
            return self.vfs.files
        picked: list[VirtualFile] = []
        seen: set[str] = set()
        for raw in scope_paths:
            needle = raw.strip().replace("\\", "/")
            if not needle.startswith("/"):
                needle = "/" + needle
            exact = self.vfs.resolve(needle)
            if exact is not None and exact.path not in seen:
                seen.add(exact.path)
                picked.append(exact)
                continue
            # 允许只给目录，前缀命中该目录下的全部文件
            prefix = needle.rstrip("/") + "/"
            for vf in self.vfs.files:
                if vf.path.startswith(prefix) and vf.path not in seen:
                    seen.add(vf.path)
                    picked.append(vf)
        return picked

    async def _render_hits(
        self,
        hits: list[_Hit],
        *,
        total: int,
        files_hit: int,
        scanned: int,
        truncated: bool,
    ) -> str:
        if not hits:
            return f"没有命中。已扫 {scanned} 个文件。"

        head = f"命中 {total} 处，分布在 {files_hit} 个文件（已扫 {scanned} 个）。"
        if truncated:
            head += f" 只列出前 {len(hits)} 处，需要更多请缩小检索词或指定 paths。"

        grouped: dict[str, list[_Hit]] = {}
        for hit in hits:
            grouped.setdefault(hit.vf.path, []).append(hit)

        blocks: list[str] = [head]
        for path, items in grouped.items():
            vf = items[0].vf
            lines = await self.vfs.lines(vf)
            blocks.append(f"\n### {path}（{vf.meeting_title} · {vf.meeting_time}）")
            for hit in items:
                url = build_link(vf, lines, hit.line_no, hit.line_no, scope=self.scope)
                blocks.append(f"- 第 {hit.line_no} 行｜命中「{hit.matched}」｜{url}")
                lo = max(1, hit.line_no - CONTEXT_LINES)
                hi = min(len(lines), hit.line_no + CONTEXT_LINES)
                for n in range(lo, hi + 1):
                    blocks.append(_numbered(lines, n, marker=n == hit.line_no))
        return "\n".join(blocks)

    # ---------- read ----------

    async def read(self, args: dict[str, Any]) -> dict[str, Any]:
        path = str(args.get("path") or "")
        vf = self.vfs.resolve(path)
        await self._started("read", f"阅读 {path}")
        if vf is None:
            await self._ended("read", "路径不存在")
            return _text_result(_unknown_path_hint(path), is_error=True)

        lines = await self.vfs.lines(vf)
        total_lines = len(lines)
        start = max(1, min(_as_int(args.get("start_line"), 1), max(1, total_lines)))
        count = max(1, min(_as_int(args.get("line_count"), READ_DEFAULT_LINES), READ_MAX_LINES))
        limit = max(200, min(_as_int(args.get("limit"), READ_DEFAULT_LIMIT), READ_MAX_LIMIT))

        end = min(total_lines, start + count - 1)
        body: list[str] = []
        used = 0
        truncated = False
        last = start - 1
        for n in range(start, end + 1):
            rendered = f"{n}| {lines[n - 1].rstrip()}"
            if used + len(rendered) > limit and body:
                truncated = True
                break
            body.append(rendered)
            used += len(rendered) + 1
            last = n

        above_lines = start - 1
        above_chars = sum(len(line) for line in lines[: start - 1])
        below_lines = total_lines - last
        below_chars = sum(len(line) for line in lines[last:])

        meta = [
            f"{vf.path}（{vf.meeting_title} · {vf.meeting_time}）",
            f"本段 第 {start}-{last} 行，共 {total_lines} 行",
            f"上方还有 {above_lines} 行 / 约 {above_chars} 字",
            f"下方还有 {below_lines} 行 / 约 {below_chars} 字",
        ]
        if truncated:
            meta.append(f"已达 limit={limit} 字，后面被截断了，继续读请把 start_line 设成 {last + 1}")

        await self._ended("read", f"读了第 {start}-{last} 行")
        return _text_result("\n".join(meta) + "\n\n" + "\n".join(body))

    # ---------- send ----------

    async def send(self, args: dict[str, Any]) -> dict[str, Any]:
        path = str(args.get("path") or "")
        vf = self.vfs.resolve(path)
        await self._started("send", f"发送 {path}")
        if vf is None:
            await self._ended("send", "路径不存在")
            return _text_result(_unknown_path_hint(path), is_error=True)

        lines = await self.vfs.lines(vf)
        total_lines = len(lines)
        start = max(1, min(_as_int(args.get("start_line"), 1), max(1, total_lines)))
        count = max(1, min(_as_int(args.get("line_count"), 1), SEND_MAX_LINES))
        end = min(total_lines, start + count - 1)

        quote = "\n".join(line.rstrip() for line in lines[start - 1 : end]).strip()
        if len(quote) > SEND_QUOTE_MAX_CHARS:
            quote = quote[:SEND_QUOTE_MAX_CHARS] + "…"
        if not quote:
            await self._ended("send", "这一段是空的")
            return _text_result(
                f"第 {start}-{end} 行是空白，没什么可发的，换个区间。", is_error=True
            )

        card = CardPayload(
            path=vf.path,
            meeting_title=vf.meeting_title,
            meeting_time=vf.meeting_time,
            start_line=start,
            end_line=end,
            quote=quote,
            url=build_link(vf, lines, start, end, scope=self.scope),
            note=str(args.get("note") or "").strip()[:200],
        )
        self.cards.append(card)
        if self.on_card is not None:
            await self.on_card(card)

        await self._ended("send", f"已发送第 {start}-{end} 行")
        return _text_result(
            f"已经把 {vf.path} 第 {start}-{end} 行连同跳转链接发给用户了，不用在回答里重复整段引文。"
        )

    # ---------- 装配 ----------

    def sdk_tools(self) -> list[Any]:
        search_tool = tool(
            "search",
            "在课程资料里检索。patterns 是检索词列表；mode=keyword 按关键字（大小写不敏感），"
            "mode=regex 按正则；logic=or 命中任一即可，logic=and 要求同一个文件里每个词都出现过。"
            "返回命中的虚拟路径、会议名、行号、跳转链接和上下各三行。",
            {
                "type": "object",
                "properties": {
                    "patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "检索词列表，最多 8 个",
                    },
                    "logic": {
                        "type": "string",
                        "enum": [LOGIC_OR, LOGIC_AND],
                        "description": "多个检索词之间的关系，默认 or",
                    },
                    "mode": {
                        "type": "string",
                        "enum": [MODE_KEYWORD, MODE_REGEX],
                        "description": "检索模式，默认 keyword",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "只在这些虚拟路径或目录里找；不填则全范围",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回多少条命中",
                    },
                },
                "required": ["patterns"],
            },
        )(self.search)

        read_tool = tool(
            "read",
            "读某个虚拟文件的一段。start_line 是起始行（从 1 开始），line_count 是往下读多少行，"
            "limit 是字符上限。返回带行号的正文，以及上下各还剩多少行多少字。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "虚拟文件路径"},
                    "start_line": {"type": "integer", "description": "起始行，默认 1"},
                    "line_count": {
                        "type": "integer",
                        "description": f"往下读多少行，默认 {READ_DEFAULT_LINES}",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"字符上限，默认 {READ_DEFAULT_LIMIT}",
                    },
                },
                "required": ["path"],
            },
        )(self.read)

        send_tool = tool(
            "send",
            "把某个虚拟文件的一段原文连同跳转链接推给用户，在对话里显示成一张卡片。"
            "用来把你找到的关键段落交给用户，发过之后回答里不用再抄一遍原文。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "虚拟文件路径"},
                    "start_line": {"type": "integer", "description": "起始行，从 1 开始"},
                    "line_count": {"type": "integer", "description": "往下取多少行"},
                    "note": {
                        "type": "string",
                        "description": "一句话说明这段为什么值得看（可选）",
                    },
                },
                "required": ["path", "start_line", "line_count"],
            },
        )(self.send)

        return [search_tool, read_tool, send_tool]


def _normalize_paths(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    return []


def _build_matchers(
    patterns: list[str], mode: str
) -> list[tuple[str, Callable[[str], bool]]]:
    matchers: list[tuple[str, Callable[[str], bool]]] = []
    for raw in patterns:
        if mode == MODE_REGEX:
            compiled = re.compile(raw, re.IGNORECASE)
            matchers.append((raw, lambda line, c=compiled: bool(c.search(line))))
        else:
            needle = raw.lower()
            matchers.append((raw, lambda line, n=needle: n in line.lower()))
    return matchers


def _unknown_path_hint(path: str) -> str:
    return (
        f"路径 {path!r} 不在可检索范围内。只能读目录清单里列出的文件，"
        "路径要一字不差地照抄，包括时间和会议名。"
    )
