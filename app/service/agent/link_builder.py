"""把虚拟文件的行区间折算成前端能定位的深链。

转写在前端是按发言段渲染的（`.transcript-segment[data-index]`），所以行号要先
折成段序号；纪要渲染成 markdown 之后行号就没了，只能带一段引文让前端自己在
DOM 里找。
"""

from __future__ import annotations

import re
from urllib.parse import quote

from app.data_model.entity.agent_chat import SCOPE_ALL
from app.service.agent.virtual_fs import KIND_TRANSCRIPT, VirtualFile

# 与前端 meeting.js / share.js 的 SPEAKER_LINE_RE 保持一致，否则段号会错位
SPEAKER_LINE_RE = re.compile(r"^(.+?)\s+(\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s*$")

# 纪要定位靠引文，太长容易因为渲染差异匹配不上
QUOTE_HINT_MAX_CHARS = 60

# markdown 里会被渲染成独立元素或被吃掉的标记，引文不能跨过它们
_MARKDOWN_MARKER_RE = re.compile(r"[`*_~\[\]()#>|]+|^\s*[-+]\s+|^\s*\d+\.\s+")


def segment_of_lines(lines: list[str]) -> list[int | None]:
    """每一行属于第几个发言段；表头行是 None。索引与前端 data-index 对齐。"""
    owner: list[int | None] = []
    current = -1
    started = False
    for line in lines:
        if SPEAKER_LINE_RE.match(line):
            current += 1
            started = True
            owner.append(current)
            continue
        owner.append(current if started else None)
    return owner


def segment_range(
    lines: list[str], start_line: int, end_line: int
) -> tuple[int, int] | None:
    """行区间（1-based，含两端）覆盖到的段序号区间。"""
    owner = segment_of_lines(lines)
    hits = [
        owner[i - 1]
        for i in range(max(1, start_line), min(len(owner), end_line) + 1)
        if owner[i - 1] is not None
    ]
    if not hits:
        return None
    return min(hits), max(hits)  # type: ignore[type-var]


def quote_hint(lines: list[str], start_line: int, end_line: int) -> str:
    """挑一段渲染后必然落在同一个文本节点里的引文，给前端做定位。

    不能简单把 markdown 标记删掉：`**粗体**` 和 `` `代码` `` 渲染后会各自成为独立
    的元素，跨过标记的字符串在 DOM 里根本不连续，找不着。所以按标记切开，挑最长
    的那一段纯文本。
    """
    best = ""
    for i in range(max(1, start_line), min(len(lines), end_line) + 1):
        raw = lines[i - 1].strip()
        if not raw:
            continue
        for fragment in _MARKDOWN_MARKER_RE.split(raw):
            cleaned = re.sub(r"\s+", " ", fragment).strip()
            if len(cleaned) > len(best):
                best = cleaned
        if len(best) >= 12:
            break
    if len(best) < 4:
        return ""
    return best[:QUOTE_HINT_MAX_CHARS]


def build_link(
    vf: VirtualFile,
    lines: list[str],
    start_line: int,
    end_line: int,
    *,
    scope: str,
) -> str:
    """站内相对链接；卡片里点一下就能跳到原文并高亮。"""
    if vf.share_token:
        base = f"/share.html?s={quote(vf.share_token, safe='')}"
    else:
        base = f"/meeting.html?token={quote(vf.minute_token, safe='')}"
        if scope == SCOPE_ALL:
            base += f"&owner_user_id={vf.owner_user_id}"

    if vf.kind == KIND_TRANSCRIPT:
        span = segment_range(lines, start_line, end_line)
        if span is None:
            return f"{base}&tab=transcript"
        first, last = span
        return f"{base}&tab=transcript&seg={first}&segs={last - first + 1}"

    hint = quote_hint(lines, start_line, end_line)
    if not hint:
        return f"{base}&tab=summary"
    return f"{base}&tab=summary&q={quote(hint, safe='')}"
