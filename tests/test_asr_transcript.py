"""自建转写文本的拼装与展示换名。

转写文件的形状必须和飞书原文一致，否则纪要提示词、时间锚点、划词提问全都读不动，
所以这里用现成的解析器回读一遍作为约束。
"""

from app.service.asr_transcript import (
    TranscriptLine,
    apply_display_names,
    build_transcript,
    format_clock,
    format_duration,
    local_label,
    merge_lines,
)
from app.service.transcript_anchor import (
    extract_unique_speakers,
    parse_transcript_segments,
)


def _line(speaker: str, start_ms: int, end_ms: int, text: str) -> TranscriptLine:
    return TranscriptLine(speaker=speaker, start_ms=start_ms, end_ms=end_ms, text=text)


def test_format_clock_and_duration_match_feishu_shape():
    assert format_clock(3_723_456) == "01:02:03.456"
    assert format_clock(-5) == "00:00:00.000"
    assert format_duration(3_600_000) == "1小时 0分钟 0秒"
    assert format_duration(63_000) == "1分钟 3秒"
    assert format_duration(9_000) == "9秒"


def test_merge_lines_joins_same_speaker_within_gap():
    merged = merge_lines(
        [
            _line("说话人1", 0, 2000, "这个方案"),
            _line("说话人1", 2500, 4000, "我们下周定。"),
            _line("说话人2", 4200, 6000, "好。"),
            _line("说话人1", 6100, 8000, "那就这样。"),
        ]
    )
    assert [item.speaker for item in merged] == ["说话人1", "说话人2", "说话人1"]
    assert merged[0].text == "这个方案我们下周定。"
    assert merged[0].end_ms == 4000


def test_merge_lines_keeps_far_apart_utterances_separate():
    merged = merge_lines(
        [
            _line("说话人1", 0, 2000, "先说到这。"),
            _line("说话人1", 30_000, 32_000, "回到刚才的话题。"),
        ]
    )
    assert len(merged) == 2


def test_merge_lines_drops_blank_text():
    assert merge_lines([_line("说话人1", 0, 1000, "  ")]) == []


def test_build_transcript_is_parseable_by_anchor_parser():
    transcript = build_transcript(
        [
            _line("说话人1", 11_161, 14_000, "先过一下上周的进展。"),
            _line("说话人2", 308_720, 312_000, "接口那块还差联调。"),
        ],
        duration_ms=3_603_000,
        create_time="2026-07-21 20:58:04 CST",
        keywords=["联调", "排期"],
    )

    assert transcript.startswith("2026-07-21 20:58:04 CST|1小时 0分钟 3秒")
    assert "关键词:" in transcript

    segments = parse_transcript_segments(transcript)
    assert [s.speaker for s in segments] == ["说话人1", "说话人2"]
    assert segments[0].start_seconds == 11
    assert segments[1].start_seconds == 308
    assert extract_unique_speakers(transcript) == ["说话人1", "说话人2"]


def test_build_transcript_without_create_time_has_no_leading_pipe():
    transcript = build_transcript(
        [_line("说话人1", 0, 1000, "开始。")], duration_ms=1000
    )
    assert transcript.startswith("1秒")


def test_apply_display_names_replaces_only_header_lines():
    transcript = build_transcript(
        [
            _line("说话人1", 0, 2000, "说话人2 这个词出现在正文里也不该被换掉。"),
            _line("说话人2", 5000, 7000, "收到。"),
        ],
        duration_ms=10_000,
    )
    named = apply_display_names(transcript, {"说话人1": "张三"})

    assert "张三 00:00:00.000" in named
    # 正文里的同名字符串不动，段头没命名的编号也原样保留
    assert "说话人2 这个词出现在正文里也不该被换掉。" in named
    assert "说话人2 00:00:05.000" in named


def test_apply_display_names_is_noop_without_mapping():
    transcript = build_transcript([_line("说话人1", 0, 1000, "在。")], duration_ms=1000)
    assert apply_display_names(transcript, {}) == transcript


def test_local_label_starts_at_one():
    assert local_label(1) == "说话人1"
    assert local_label(12) == "说话人12"
