"""本地导入的文档解析。

下游一切都按飞书妙记那套 `说话人1 HH:MM:SS.mmm` 的形状读转写，所以这里的验收点
不是「文字有没有搬过来」，而是「搬过来之后还长得像飞书原文」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.service.document_transcript import DocumentParseError, parse_document
from app.service.transcript_anchor import extract_unique_speakers, parse_transcript_segments


def test_plain_text_splits_on_blank_lines(tmp_path: Path):
    path = tmp_path / "note.txt"
    path.write_text("第一段说了点什么。\n\n第二段又说了点别的。\n", encoding="utf-8")

    parsed = parse_document(path)

    assert parsed.segments == 2
    assert parsed.exact_duration is False
    assert "第一段说了点什么。" in parsed.transcript
    assert "第二段又说了点别的。" in parsed.transcript
    segments = parse_transcript_segments(parsed.transcript)
    # 假时间轴必须递增，否则锚点跳转会全落在开头
    assert segments[1].start_seconds > segments[0].start_seconds


def test_text_without_blank_lines_falls_back_to_per_line(tmp_path: Path):
    path = tmp_path / "note.md"
    path.write_text("# 标题\n第一行\n第二行\n", encoding="utf-8")

    parsed = parse_document(path)

    assert parsed.segments == 3


def test_srt_keeps_its_real_timeline(tmp_path: Path):
    path = tmp_path / "sub.srt"
    path.write_text(
        "1\n00:00:02,000 --> 00:00:05,500\n开场白\n\n"
        "2\n00:01:10,250 --> 00:01:15,000\n后面这句\n",
        encoding="utf-8",
    )

    parsed = parse_document(path)

    assert parsed.exact_duration is True
    assert parsed.duration_ms == 75_000
    segments = parse_transcript_segments(parsed.transcript)
    assert [s.start_seconds for s in segments] == [2, 70]


def test_srt_without_timeline_degrades_to_paragraphs(tmp_path: Path):
    """时间轴解析不出来时按普通文本收，总比整份丢掉好。"""
    path = tmp_path / "broken.srt"
    path.write_text("就是一份没有时间轴的文本\n\n第二段\n", encoding="utf-8")

    parsed = parse_document(path)

    assert parsed.exact_duration is False
    assert parsed.segments == 2


def test_gbk_text_is_decoded_instead_of_mojibake(tmp_path: Path):
    path = tmp_path / "gbk.txt"
    path.write_bytes("中文内容不该变乱码".encode("gb18030"))

    parsed = parse_document(path)

    assert "中文内容不该变乱码" in parsed.transcript


def test_speakers_are_parseable_by_the_shared_helper(tmp_path: Path):
    """参会人解析读的是段头，导入的转写也得能被它认出来。"""
    path = tmp_path / "note.txt"
    path.write_text("一段话\n\n又一段话\n", encoding="utf-8")

    parsed = parse_document(path)

    assert extract_unique_speakers(parsed.transcript) == ["说话人1"]


def test_empty_document_is_rejected(tmp_path: Path):
    path = tmp_path / "empty.txt"
    path.write_text("   \n\n  \n", encoding="utf-8")

    with pytest.raises(DocumentParseError, match="没有可用的文字"):
        parse_document(path)


def test_unsupported_suffix_is_rejected(tmp_path: Path):
    path = tmp_path / "sheet.xlsx"
    path.write_bytes(b"whatever")

    with pytest.raises(DocumentParseError, match="不支持"):
        parse_document(path)
