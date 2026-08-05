from app.service.transcript_anchor import (
    align_summary_anchors,
    format_seconds,
    parse_seconds,
    parse_transcript_segments,
)

TRANSCRIPT = """2026-07-21 20:58:04 CST|1小时 3秒

关键词:
向量、链路

同学甲 00:00:11.161
第一段发言内容。

同学乙 00:05:08.720
第二段发言内容。

同学甲 00:34:52.003
第三段发言内容。
"""


def test_parse_transcript_segments_extracts_start_times():
    segments = parse_transcript_segments(TRANSCRIPT)
    assert [s.start_seconds for s in segments] == [11, 308, 2092]
    assert [s.speaker for s in segments] == ["同学甲", "同学乙", "同学甲"]


def test_parse_transcript_segments_ignores_metadata_lines():
    # 首行的 "2026-07-21 20:58:04 CST|1小时 3秒" 不带毫秒，不应被当成发言段落
    assert len(parse_transcript_segments(TRANSCRIPT)) == 3


def test_existing_anchor_is_left_untouched():
    segments = parse_transcript_segments(TRANSCRIPT)
    result = align_summary_anchors("讲了 token `00:05:08` 的定义。", segments)
    assert result.text == "讲了 token `00:05:08` 的定义。"
    assert result.total == 1
    assert result.aligned == 0


def test_rounded_anchor_is_snapped_to_nearest_segment():
    segments = parse_transcript_segments(TRANSCRIPT)
    result = align_summary_anchors("向量模型 `00:35:00` 的部分。", segments)
    assert result.text == "向量模型 `00:34:52` 的部分。"
    assert result.aligned == 1
    assert result.suspicious == []


def test_far_off_anchor_is_snapped_but_flagged_suspicious():
    segments = parse_transcript_segments(TRANSCRIPT)
    result = align_summary_anchors("凭空捏造的 `01:20:00` 时间点。", segments)
    assert result.text == "凭空捏造的 `00:34:52` 时间点。"
    assert result.suspicious == ["01:20:00"]


def test_multiple_anchors_are_counted_together():
    segments = parse_transcript_segments(TRANSCRIPT)
    result = align_summary_anchors("`00:00:11` 与 `00:05:10` 与 `00:34:52`", segments)
    assert result.total == 3
    assert result.aligned == 1
    assert "`00:05:08`" in result.text


def test_empty_segments_returns_summary_unchanged():
    original = "没有转写可对齐 `00:12:34`"
    result = align_summary_anchors(original, [])
    assert result.text == original
    assert result.total == 0
    assert result.hit_rate == 1.0


def test_parse_and_format_seconds_round_trip():
    assert parse_seconds("01:02:03") == 3723
    assert format_seconds(3723) == "01:02:03"
    assert parse_seconds("bad") is None
    assert parse_seconds("1:2") is None


def test_parse_seconds_accepts_transcript_milliseconds():
    """规划模型常按提示词把转写里的 时:分:秒.毫秒 原样抄进 timestamp。"""
    assert parse_seconds("00:14:00.189") == 14 * 60
    assert parse_seconds("01:06:47.579") == 1 * 3600 + 6 * 60 + 47
    assert parse_seconds(" 00:02:52.400 ") == 2 * 60 + 52
