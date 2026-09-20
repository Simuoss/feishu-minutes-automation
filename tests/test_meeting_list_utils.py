"""列表排序用的开会时间解析：飞书入库经常是毫秒时间戳字符串。"""

from app.service.meeting_list_utils import create_time_sort_key, parse_create_time


def test_parse_create_time_accepts_epoch_ms():
    # 2026-08-11 20:00 +08 = 2026-08-11 12:00 UTC
    parsed = parse_create_time("1786449628826")
    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.month == 8
    assert parsed.day == 11


def test_parse_create_time_accepts_epoch_seconds():
    parsed = parse_create_time("1700000000")
    assert parsed is not None
    assert parsed.year == 2023


def test_sort_key_orders_epoch_and_iso():
    older = create_time_sort_key("1700000000000")
    newer = create_time_sort_key("1786449628826")
    iso = create_time_sort_key("2026-09-01 10:00:00")
    assert older < newer
    assert newer < iso
    assert older > 0
