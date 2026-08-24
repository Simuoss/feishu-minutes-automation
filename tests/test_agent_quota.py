"""每日配额：公开访客的上限、日切归零、不限额主体照样计数。"""

from __future__ import annotations

import asyncio

import pytest

from app.data_model.entity.agent_chat import (
    SUBJECT_ACCESS_KEY,
    SUBJECT_ANON,
    SUBJECT_USER,
)
from app.service.agent import quota_service
from app.service.agent.quota_service import AgentQuotaExceeded, Subject


def test_anonymous_subject_is_share_session_plus_ip():
    same = Subject.for_anonymous("sess-1", "1.2.3.4")
    again = Subject.for_anonymous("sess-1", "1.2.3.4")
    other_ip = Subject.for_anonymous("sess-1", "5.6.7.8")
    other_session = Subject.for_anonymous("sess-2", "1.2.3.4")

    assert same == again
    assert same != other_ip
    assert same != other_session
    assert same.type == SUBJECT_ANON


def test_key_subject_ignores_the_order_the_keys_came_in():
    a = Subject.for_access_keys(["h2", "h1"])
    b = Subject.for_access_keys(["h1", "h2", "h1"])

    assert a == b
    assert a.type == SUBJECT_ACCESS_KEY


def test_default_limits_come_from_settings():
    assert quota_service.limit_for(SUBJECT_ANON) == 20
    assert quota_service.limit_for(SUBJECT_ACCESS_KEY) == 0
    assert quota_service.limit_for(SUBJECT_USER) == 0


@pytest.mark.usefixtures("_memory_db")
def test_public_guest_gets_cut_off_at_twenty_a_day():
    subject = Subject.for_anonymous("sess-1", "1.2.3.4")

    async def scenario() -> None:
        for n in range(20):
            state = await quota_service.consume(subject)
            assert state.used == n + 1
        with pytest.raises(AgentQuotaExceeded) as caught:
            await quota_service.consume(subject)
        assert caught.value.state.remaining == 0
        assert "用完" in str(caught.value)

    asyncio.run(scenario())


@pytest.mark.usefixtures("_memory_db")
def test_the_counter_starts_over_the_next_day(monkeypatch: pytest.MonkeyPatch):
    subject = Subject.for_anonymous("sess-1", "1.2.3.4")

    async def scenario() -> None:
        monkeypatch.setattr(quota_service, "today", lambda: "2026-08-21")
        for _ in range(20):
            await quota_service.consume(subject)
        with pytest.raises(AgentQuotaExceeded):
            await quota_service.consume(subject)

        monkeypatch.setattr(quota_service, "today", lambda: "2026-08-22")
        state = await quota_service.consume(subject)
        assert state.used == 1

    asyncio.run(scenario())


@pytest.mark.usefixtures("_memory_db")
def test_unlimited_subjects_are_still_counted_so_usage_stays_visible():
    subject = Subject.for_user(7)

    async def scenario() -> None:
        for n in range(3):
            state = await quota_service.consume(subject)
            assert state.unlimited
            assert state.remaining is None
            assert state.used == n + 1
        seen = await quota_service.peek(subject)
        assert seen.used == 3

    asyncio.run(scenario())


@pytest.mark.usefixtures("_memory_db")
def test_two_guests_on_different_ips_do_not_share_a_bucket():
    first = Subject.for_anonymous("sess-1", "1.2.3.4")
    second = Subject.for_anonymous("sess-1", "9.9.9.9")

    async def scenario() -> None:
        for _ in range(20):
            await quota_service.consume(first)
        state = await quota_service.consume(second)
        assert state.used == 1

    asyncio.run(scenario())
