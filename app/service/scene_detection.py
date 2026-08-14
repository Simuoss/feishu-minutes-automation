"""判定一份转写属于讲课还是会议，决定后续用哪套提示词。

判定失败不降级：两套模板的结构差得很远，猜错场景写出来的纪要通篇跑偏，
代价高于让用户重跑一次。因此这里只抛错，由上层把任务标记为失败。

送进模型的不是全文：判定所需的信息集中在开头与中段，再配上代码算出的发言
分布（两人且一方占绝大多数 → 多半是讲课；多人且分布分散 → 多半是会议）。
发言统计只作为证据交给模型，不在代码里做硬判定。
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from app.integrations.llm.messages_client import LlmClient
from app.service.summary_illustration import extract_json_object
from app.service.summary_scene import (
    SCENE_LABELS,
    VALID_SCENES,
    Scene,
    load_prompt,
)
from app.service.transcript_anchor import SEGMENT_HEAD_RE

logger = logging.getLogger(__name__)

DETECT_PROMPT_NAME = "scene_detect.md"
# 判定结果本身只有一两百字，但快模型会先花掉一段思考预算，给窄了会连 JSON 都吐不完
DETECT_MAX_TOKENS = 4096

HEAD_CHARS = 3000
MIDDLE_CHARS = 1500


class SceneDetectionError(RuntimeError):
    """场景判定失败，无法决定用哪套提示词。"""


@dataclass
class SceneDecision:
    scene: Scene
    reason: str

    @property
    def label(self) -> str:
        return SCENE_LABELS[self.scene]


@dataclass
class SpeakerStat:
    name: str
    segments: int
    chars: int


def speaker_stats(transcript: str) -> list[SpeakerStat]:
    """按说话人统计段落数与发言字数，字数降序。"""
    heads = list(SEGMENT_HEAD_RE.finditer(transcript))
    totals: dict[str, SpeakerStat] = {}
    for index, head in enumerate(heads):
        name = head.group(1).strip()
        if not name:
            continue
        body_end = heads[index + 1].start() if index + 1 < len(heads) else len(transcript)
        body = transcript[head.end() : body_end].strip()
        stat = totals.get(name)
        if stat is None:
            stat = SpeakerStat(name=name, segments=0, chars=0)
            totals[name] = stat
        stat.segments += 1
        stat.chars += len(body)
    return sorted(totals.values(), key=lambda s: (-s.chars, s.name))


def _speakers_block(transcript: str) -> str:
    stats = speaker_stats(transcript)
    if not stats:
        return (
            "<speakers>\n"
            "未能从转写中解析出发言人分段，无法提供发言分布，请仅依据转写内容判断。\n"
            "</speakers>"
        )
    total = sum(s.chars for s in stats) or 1
    lines = [f"共 {len(stats)} 位发言人，按发言字数降序："]
    lines.extend(
        f"- {s.name}：{s.segments} 段 / {s.chars} 字 / {round(s.chars * 100 / total)}%"
        for s in stats
    )
    return "<speakers>\n" + "\n".join(lines) + "\n</speakers>"


def build_evidence(transcript: str) -> str:
    """拼出判定用的输入：发言分布 + 开头 + 中段抽样。"""
    blocks = [_speakers_block(transcript)]
    blocks.append(f"<transcript_head>\n{transcript[:HEAD_CHARS]}\n</transcript_head>")
    # 开头常是寒暄与设备调试，短录音之外都再给一段中间内容，避免只看开头判错
    if len(transcript) > HEAD_CHARS + MIDDLE_CHARS:
        start = max(HEAD_CHARS, (len(transcript) - MIDDLE_CHARS) // 2)
        middle = transcript[start : start + MIDDLE_CHARS]
        blocks.append(f"<transcript_middle>\n{middle}\n</transcript_middle>")
    return "\n\n".join(blocks)


class SceneDetector:
    def __init__(self, llm: LlmClient) -> None:
        self._llm = llm

    async def detect(
        self,
        transcript: str,
        *,
        on_attempt: Callable[[int, int], None] | None = None,
    ) -> SceneDecision:
        completion = await self._llm.complete(
            load_prompt(DETECT_PROMPT_NAME),
            build_evidence(transcript),
            max_tokens=DETECT_MAX_TOKENS,
            on_attempt=on_attempt,
        )
        payload = extract_json_object(completion.text)
        if payload is None:
            logger.error(
                "场景判定失败：模型没有返回 JSON，怀疑它改用自然语言作答。原始输出: %s",
                completion.text[:300],
            )
            raise SceneDetectionError("模型未按约定返回场景判定结果")

        raw = str(payload.get("scene") or "").strip().upper()
        if raw not in VALID_SCENES:
            logger.error(
                "场景判定失败：模型返回了未知场景 %r，允许的取值只有 %s",
                raw,
                "/".join(VALID_SCENES),
            )
            raise SceneDetectionError(f"模型返回了无法识别的场景取值 {raw or '（空）'}")

        scene = cast(Scene, raw)
        reason = str(payload.get("reason") or "").strip()
        logger.info(
            "场景判定为 %s（%s）。依据：%s",
            scene,
            SCENE_LABELS[scene],
            reason or "（未说明）",
        )
        return SceneDecision(scene=scene, reason=reason)
