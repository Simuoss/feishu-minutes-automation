"""分享库 create_time 回填目标挑选。"""

from app.service.create_time_backfill import pick_create_time_targets


def test_pick_skips_already_filled_and_dedupes():
    pairs = [
        (1, "tok-a"),
        (1, "tok-a"),
        (1, "tok-b"),
        (2, "tok-c"),
    ]
    known = {
        (1, "tok-a"): "2026-08-01 10:00:00",
        (1, "tok-b"): None,
    }
    assert pick_create_time_targets(pairs, known) == [(1, "tok-b"), (2, "tok-c")]


def test_pick_respects_limit():
    pairs = [(1, f"tok-{i}") for i in range(5)]
    assert pick_create_time_targets(pairs, {}, limit=2) == [(1, "tok-0"), (1, "tok-1")]
