"""场景判定：发言统计、送进模型的证据、以及判定结果的校验。"""

import asyncio
import json

import pytest

from app.integrations.llm.messages_client import LlmCompletion
from app.service.scene_detection import (
    HEAD_CHARS,
    SceneDetectionError,
    SceneDetector,
    build_evidence,
    speaker_stats,
)
from app.service.summary_scene import (
    load_scene_prompt,
    load_summary_prompt,
    normalize_scene,
)

TRANSCRIPT = """2026-08-14 10:00:00 CST|52分钟

关键词:
排期、人力

张三 00:00:11.161
我们今天过一下三季度的排期。

李四 00:00:30.400
测试这块我这边还差一个人。

张三 00:01:02.003
那从 B 项目抽调王五两周。
"""


class StubLlm:
    def __init__(self, text: str) -> None:
        self._text = text
        self.system_prompts: list[str] = []
        self.user_contents: list[str] = []

    async def complete(self, system_prompt, user_content, **kwargs):
        self.system_prompts.append(str(system_prompt))
        self.user_contents.append(str(user_content))
        return LlmCompletion(
            text=self._text,
            input_tokens=100,
            output_tokens=20,
            stop_reason="end_turn",
            attempts=1,
            elapsed_seconds=1.0,
        )


def _detect(text: str):
    return asyncio.run(SceneDetector(StubLlm(text)).detect(TRANSCRIPT))


def test_speaker_stats_counts_body_chars_per_speaker():
    stats = {s.name: s for s in speaker_stats(TRANSCRIPT)}

    assert set(stats) == {"张三", "李四"}
    assert stats["张三"].segments == 2
    assert stats["李四"].segments == 1
    # 字数只算正文，不含说话人与时间那一行
    assert stats["张三"].chars == len("我们今天过一下三季度的排期。") + len("那从 B 项目抽调王五两周。")


def test_speaker_stats_sorted_by_chars_desc():
    assert [s.name for s in speaker_stats(TRANSCRIPT)] == ["张三", "李四"]


def test_evidence_carries_speaker_distribution_and_head():
    evidence = build_evidence(TRANSCRIPT)

    assert "<speakers>" in evidence
    assert "共 2 位发言人" in evidence
    assert "张三：2 段" in evidence
    assert "<transcript_head>" in evidence
    # 短转写不必再抽中段
    assert "<transcript_middle>" not in evidence


def test_evidence_samples_middle_for_long_transcript():
    """开头常是寒暄，长录音必须再给一段中间内容。"""
    long_transcript = TRANSCRIPT + "王五 00:02:00.000\n" + "补充说明。" * 2000

    evidence = build_evidence(long_transcript)

    assert "<transcript_middle>" in evidence
    assert len(evidence) < len(long_transcript)


def test_evidence_survives_unparsable_transcript():
    evidence = build_evidence("没有任何说话人分段的纯文本")

    assert "未能从转写中解析出发言人分段" in evidence
    assert "<transcript_head>" in evidence


def test_evidence_truncates_head():
    long_transcript = "甲 00:00:01.000\n" + "话" * (HEAD_CHARS * 3)

    evidence = build_evidence(long_transcript)

    assert len(evidence) < len(long_transcript)


def test_detect_reads_scene_and_reason():
    decision = _detect(
        json.dumps({"scene": "MEETING", "reason": "多人过进度"}, ensure_ascii=False)
    )

    assert decision.scene == "MEETING"
    assert decision.label == "会议"
    assert decision.reason == "多人过进度"


def test_detect_accepts_lowercase_and_code_fence():
    decision = _detect('```json\n{"scene": "lecture", "reason": "一人讲"}\n```')

    assert decision.scene == "LECTURE"


def test_detect_rejects_unknown_scene():
    with pytest.raises(SceneDetectionError):
        _detect(json.dumps({"scene": "TRAINING", "reason": "像培训"}))


def test_detect_rejects_missing_scene():
    with pytest.raises(SceneDetectionError):
        _detect(json.dumps({"reason": "说不好"}))


def test_detect_rejects_prose_answer():
    with pytest.raises(SceneDetectionError):
        _detect("我觉得这更像是一场会议。")


def test_detect_sends_evidence_not_full_transcript():
    llm = StubLlm(json.dumps({"scene": "MEETING", "reason": "多人"}))

    asyncio.run(SceneDetector(llm).detect(TRANSCRIPT))

    assert "<speakers>" in llm.user_contents[0]
    # 提示词必须给出封闭取值，否则模型会自造分类
    assert "LECTURE" in llm.system_prompts[0]
    assert "MEETING" in llm.system_prompts[0]


def test_normalize_scene_filters_unknown_values():
    assert normalize_scene("meeting") == "MEETING"
    assert normalize_scene(" LECTURE ") == "LECTURE"
    assert normalize_scene("TRAINING") is None
    assert normalize_scene(None) is None


def test_summary_prompts_differ_by_scene():
    lecture = load_summary_prompt("LECTURE")
    meeting = load_summary_prompt("MEETING")

    assert "课后复习" in lecture
    assert "行动项" in meeting
    assert "本次未形成决议" in meeting


def test_scene_section_is_injected_into_shared_base():
    lecture_plan = load_scene_prompt("screenshot_plan", "LECTURE")
    meeting_plan = load_scene_prompt("screenshot_plan", "MEETING")

    # 共用段落两边都在，差异段各不相同
    assert "screen_shared" in lecture_plan and "screen_shared" in meeting_plan
    assert "{{SCENE_SECTION}}" not in lecture_plan
    assert "{{SCENE_SECTION}}" not in meeting_plan
    assert "指示代词密集处" in lecture_plan
    assert "指示代词密集处" not in meeting_plan
    assert "被逐条讨论的材料页" in meeting_plan


def test_addendum_scene_sections_are_injected():
    lecture = load_scene_prompt("illustrated_addendum", "LECTURE")
    meeting = load_scene_prompt("illustrated_addendum", "MEETING")

    assert "{{SCENE_SECTION}}" not in lecture
    assert "{{SCENE_SECTION}}" not in meeting
    assert "「你问过的问题」" in lecture
    assert "不要为「行动项」配图" in meeting


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
