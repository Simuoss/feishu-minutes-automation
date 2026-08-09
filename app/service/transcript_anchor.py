"""转写段落解析与纪要时间锚点校正。

模型生成的 `HH:MM:SS` 锚点偶尔会被取整（如把 00:34:52 写成 00:35:00），
导致前端点击后跳到一个并不存在的位置。这里统一把锚点吸附到转写中真实存在的
段落起点，保证锚点全部可跳转。
"""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SEGMENT_HEAD_RE = re.compile(r"^(.+?)\s+(\d{1,2}):(\d{2}):(\d{2})\.\d{1,3}\s*$", re.MULTILINE)
ANCHOR_RE = re.compile(r"`(\d{1,2}:\d{2}:\d{2})`")

# 吸附距离超过该阈值时，锚点大概率不是取整误差而是模型凭空生成
SUSPICIOUS_GAP_SECONDS = 180


@dataclass
class TranscriptSegment:
    start_seconds: int
    speaker: str


@dataclass
class AnchorAlignment:
    text: str
    total: int = 0
    aligned: int = 0
    suspicious: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        if self.total == 0:
            return 1.0
        return (self.total - self.aligned) / self.total


def format_seconds(total_seconds: int) -> str:
    hours, remainder = divmod(max(0, total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_seconds(value: str) -> int | None:
    """解析 HH:MM:SS；兼容转写原样抄来的毫秒（如 00:14:00.189）。"""
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        # 飞书转写段落是「时:分:秒.毫秒」；规划模型常按提示词逐字照抄，秒段会带小数
        seconds = int(float(parts[2]))
    except ValueError:
        return None
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def parse_transcript_segments(transcript: str) -> list[TranscriptSegment]:
    """从飞书妙记转写中解析出所有发言段落的起始时间。"""
    segments = [
        TranscriptSegment(
            start_seconds=int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4)),
            speaker=m.group(1).strip(),
        )
        for m in SEGMENT_HEAD_RE.finditer(transcript)
    ]
    segments.sort(key=lambda s: s.start_seconds)
    return segments


def extract_unique_speakers(transcript: str) -> list[str]:
    """按转写出现顺序去重参会发言人，供列表筛选。"""
    names: list[str] = []
    seen: set[str] = set()
    for seg in parse_transcript_segments(transcript or ""):
        name = (seg.speaker or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def align_summary_anchors(summary: str, segments: list[TranscriptSegment]) -> AnchorAlignment:
    """把纪要中的时间锚点吸附到最接近的真实段落起点。"""
    if not segments:
        logger.warning("转写未解析出任何发言段落，跳过锚点校正，怀疑转写格式与飞书妙记不一致")
        return AnchorAlignment(text=summary)

    valid_seconds = sorted({s.start_seconds for s in segments})
    result = AnchorAlignment(text=summary)
    valid_set = set(valid_seconds)

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        result.total += 1
        seconds = parse_seconds(raw)
        if seconds is None or seconds in valid_set:
            return match.group(0)

        nearest = min(valid_seconds, key=lambda v: abs(v - seconds))
        gap = abs(nearest - seconds)
        result.aligned += 1
        if gap > SUSPICIOUS_GAP_SECONDS:
            result.suspicious.append(raw)
        return f"`{format_seconds(nearest)}`"

    result.text = ANCHOR_RE.sub(replace, summary)

    if result.aligned:
        logger.info(
            "纪要锚点校正完成：共 %d 个，吸附 %d 个到最近段落起点，其中偏差超过 %ds 的可疑锚点 %d 个",
            result.total,
            result.aligned,
            SUSPICIOUS_GAP_SECONDS,
            len(result.suspicious),
        )
    if result.suspicious:
        logger.warning(
            "纪要中有 %d 个锚点与任何发言段落都相距甚远，怀疑模型凭空生成了时间点: %s",
            len(result.suspicious),
            ", ".join(result.suspicious),
        )
    return result
