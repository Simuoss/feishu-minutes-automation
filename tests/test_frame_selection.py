"""帧筛选纯函数：空白帧剔除、信息密度排序、跨图去重。"""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.service.frame_selection import (
    analyze_frame,
    hamming_distance,
    is_duplicate,
    pick_best_frame,
    select_distinct_frames,
)


def _write(path: Path, painter=None, color=(0, 0, 0)) -> Path:
    image = Image.new("RGB", (640, 360), color)
    if painter:
        painter(ImageDraw.Draw(image))
    image.save(path, "JPEG", quality=92)
    return path


def _text_painter(lines: int, offset: int = 0):
    def paint(draw: ImageDraw.ImageDraw) -> None:
        for i in range(lines):
            y = 10 + i * 12 + offset
            draw.rectangle([20, y, 600, y + 6], fill=(255, 255, 255))

    return paint


def test_blank_frame_is_detected(tmp_path: Path):
    stats = analyze_frame(_write(tmp_path / "black.jpg"), 10.0)
    assert stats is not None
    assert stats.is_blank


def test_frame_with_content_is_not_blank(tmp_path: Path):
    stats = analyze_frame(_write(tmp_path / "text.jpg", _text_painter(20)), 10.0)
    assert stats is not None
    assert not stats.is_blank


def test_unreadable_file_returns_none(tmp_path: Path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not an image")
    assert analyze_frame(broken, 1.0) is None


def test_pick_best_prefers_denser_frame(tmp_path: Path):
    sparse = analyze_frame(_write(tmp_path / "sparse.jpg", _text_painter(2)), 1.0)
    dense = analyze_frame(_write(tmp_path / "dense.jpg", _text_painter(24)), 2.0)
    assert sparse is not None and dense is not None

    best = pick_best_frame([sparse, dense])
    assert best is dense


def test_pick_best_returns_none_when_all_blank(tmp_path: Path):
    blanks = [
        analyze_frame(_write(tmp_path / f"b{i}.jpg", color=(i * 3, i * 3, i * 3)), float(i))
        for i in range(3)
    ]
    assert all(b is not None for b in blanks)
    assert pick_best_frame([b for b in blanks if b is not None]) is None


def test_hamming_distance_counts_differing_bits():
    assert hamming_distance(0b1011, 0b1011) == 0
    assert hamming_distance(0b1011, 0b1000) == 2


def test_is_duplicate_respects_threshold():
    assert is_duplicate(0b1011, [0b1000], threshold=2)
    assert not is_duplicate(0b1011, [0b1000], threshold=1)
    assert not is_duplicate(0b1011, [], threshold=8)


def test_select_distinct_skips_identical_screens(tmp_path: Path):
    """同一页幻灯片停留数分钟时，只应留下一张。"""
    first = analyze_frame(_write(tmp_path / "s1.jpg", _text_painter(20)), 10.0)
    same = analyze_frame(_write(tmp_path / "s2.jpg", _text_painter(20)), 120.0)
    assert first is not None and same is not None

    selected = select_distinct_frames([[first], [same]])
    assert [s.seconds for s in selected] == [10.0]


def test_select_distinct_keeps_different_screens(tmp_path: Path):
    def blocks(count: int):
        def paint(draw: ImageDraw.ImageDraw) -> None:
            for i in range(count):
                draw.rectangle([30 * i + 10, 40, 30 * i + 34, 300], fill=(255, 255, 255))

        return paint

    left = analyze_frame(_write(tmp_path / "d1.jpg", blocks(2)), 10.0)
    right = analyze_frame(_write(tmp_path / "d2.jpg", _text_painter(18)), 60.0)
    assert left is not None and right is not None

    selected = select_distinct_frames([[left], [right]])
    assert len(selected) == 2


def test_select_distinct_honours_limit(tmp_path: Path):
    groups = []
    for i in range(4):
        stats = analyze_frame(
            _write(tmp_path / f"l{i}.jpg", _text_painter(6 + i * 4, offset=i * 3)),
            float(i * 30),
        )
        assert stats is not None
        groups.append([stats])

    selected = select_distinct_frames(groups, limit=2)
    assert len(selected) <= 2


def test_select_distinct_skips_blank_groups(tmp_path: Path):
    blank = analyze_frame(_write(tmp_path / "blank.jpg"), 5.0)
    useful = analyze_frame(_write(tmp_path / "useful.jpg", _text_painter(20)), 50.0)
    assert blank is not None and useful is not None

    selected = select_distinct_frames([[blank], [useful]])
    assert [s.seconds for s in selected] == [50.0]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
