"""把语音识别的分句拼成飞书妙记同款的转写文本。

格式必须和飞书一致（`说话人 HH:MM:SS.mmm` 加正文），因为纪要提示词、时间锚点
校正、划词提问、参会人解析全都按这个格式读。文件里写的是「说话人N」这种稳定编号，
真名在读取时按声纹库替换，这样超管改一次名，历史转写立刻跟着变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 同一个人连续说话时，间隔小于该秒数就并进同一段，避免转写碎成上千行
MERGE_GAP_SECONDS = 2.0
MERGE_MAX_CHARS = 300

LOCAL_LABEL_PREFIX = "说话人"
LOCAL_LABEL_RE = re.compile(rf"^{LOCAL_LABEL_PREFIX}(\d+)$")


@dataclass
class TranscriptLine:
    speaker: str
    start_ms: int
    end_ms: int
    text: str


def local_label(index: int) -> str:
    """会议内说话人的稳定编号，从 1 开始。"""
    return f"{LOCAL_LABEL_PREFIX}{index}"


def format_clock(milliseconds: int) -> str:
    total = max(0, milliseconds)
    hours, rest = divmod(total, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def format_duration(duration_ms: int) -> str:
    """飞书头部那种「34分钟 20秒」。"""
    total_seconds = max(0, duration_ms) // 1000
    hours, rest = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}小时")
    if minutes or hours:
        parts.append(f"{minutes}分钟")
    parts.append(f"{seconds}秒")
    return " ".join(parts)


def merge_lines(lines: list[TranscriptLine]) -> list[TranscriptLine]:
    merged: list[TranscriptLine] = []
    for line in sorted(lines, key=lambda x: (x.start_ms, x.end_ms)):
        if not line.text.strip():
            continue
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous.speaker == line.speaker
            and line.start_ms - previous.end_ms <= MERGE_GAP_SECONDS * 1000
            and len(previous.text) + len(line.text) <= MERGE_MAX_CHARS
        ):
            previous.text = f"{previous.text}{line.text}"
            previous.end_ms = max(previous.end_ms, line.end_ms)
            continue
        merged.append(
            TranscriptLine(
                speaker=line.speaker,
                start_ms=line.start_ms,
                end_ms=line.end_ms,
                text=line.text.strip(),
            )
        )
    return merged


def build_transcript(
    lines: list[TranscriptLine],
    *,
    duration_ms: int,
    create_time: str | None = None,
    keywords: list[str] | None = None,
) -> str:
    body_lines: list[str] = []
    header = f"{(create_time or '').strip()}|{format_duration(duration_ms)}".lstrip("|")
    body_lines.append(header)
    body_lines.append("")
    if keywords:
        body_lines.append("关键词:")
        body_lines.append("、".join(keywords))
        body_lines.append("")
    for line in merge_lines(lines):
        # 飞书原文的时间戳后面带一个空格，解析正则依赖这行的整体形状
        body_lines.append(f"{line.speaker} {format_clock(line.start_ms)} ")
        body_lines.append(line.text)
        body_lines.append("")
    return "\n".join(body_lines).rstrip() + "\n"


def apply_display_names(transcript: str, names: dict[str, str]) -> str:
    """把转写里的「说话人N」换成声纹库里的真名。

    只替换段落头那一行的说话人字段，正文里出现的同名字符串不动。
    """
    if not names:
        return transcript

    def _replace(match: re.Match[str]) -> str:
        speaker = match.group(1)
        display = names.get(speaker)
        if not display:
            return match.group(0)
        return f"{display} {match.group(2)}"

    pattern = re.compile(
        rf"^({LOCAL_LABEL_PREFIX}\d+)\s+(\d{{1,2}}:\d{{2}}:\d{{2}}\.\d{{1,3}}\s*)$",
        re.MULTILINE,
    )
    return pattern.sub(_replace, transcript)


def apply_display_names_everywhere(text: str, names: dict[str, str]) -> str:
    """任意位置的「说话人N」都换成真名，供纪要这类散文使用。

    必须整体匹配「说话人」加数字再查表：逐个字符串替换的话，「说话人1」那条规则
    会啃掉「说话人10」的前四个字，剩一个孤零零的 0。
    """
    if not names or not text:
        return text

    def _replace(match: re.Match[str]) -> str:
        display = names.get(match.group(0))
        return display or match.group(0)

    return re.sub(rf"{LOCAL_LABEL_PREFIX}\d+", _replace, text)
