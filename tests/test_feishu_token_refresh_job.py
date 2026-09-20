"""飞书 refresh 日巡：只挑临期两天内（或旧行没记到期）的账号。"""

from types import SimpleNamespace

from app.integrations.feishu.user_auth import refresh_token_is_due
from app.service.feishu_token_refresh_job import pick_due_refresh_user_ids


def test_refresh_due_enters_two_day_window():
    now = 1_800_000_000
    assert refresh_token_is_due(now + 86400, now=now) is True
    assert refresh_token_is_due(now + 3 * 86400, now=now) is False
    assert refresh_token_is_due(None, now=now) is False
    assert refresh_token_is_due(None, now=now, unknown_is_due=True) is True


def test_refresh_due_accepts_millisecond_expiry():
    now = 1_800_000_000
    assert refresh_token_is_due((now + 3600) * 1000, now=now) is True


def test_pick_due_is_serial_and_skips_empty_refresh():
    now = 1_800_000_000
    rows = [
        SimpleNamespace(user_id=1, refresh_token="a", refresh_expires_at=now + 3600),
        SimpleNamespace(user_id=2, refresh_token="b", refresh_expires_at=now + 10 * 86400),
        SimpleNamespace(user_id=3, refresh_token="", refresh_expires_at=now + 3600),
        SimpleNamespace(user_id=4, refresh_token="d", refresh_expires_at=None),
        SimpleNamespace(user_id=1, refresh_token="a2", refresh_expires_at=now + 60),
    ]
    assert pick_due_refresh_user_ids(rows, now=now) == [1, 4]
