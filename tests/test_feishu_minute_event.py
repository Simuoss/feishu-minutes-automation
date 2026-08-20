"""妙记生成事件 → 下载 的路由与未就绪重试。"""

from __future__ import annotations

import asyncio
from typing import Any, Self

import pytest

from app.integrations.feishu.events import EVENT_MINUTE_GENERATED
from app.service.feishu_event_service import FeishuEventService
from app.service.meeting_download_service import DownloadResult, MeetingDownloadService


class FakeDownload:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = DownloadResult(
            minute_token="obcnq3b9jl72l83w4f14xxxx",
            record_id=7,
            status="COMPLETED",
        )

    async def download_minute(self, minute_token: str, **kwargs: Any) -> DownloadResult:
        self.calls.append({"minute_token": minute_token, **kwargs})
        return self.result


def _payload(
    *,
    event_id: str = "evt-1",
    minute_token: str = "obcnq3b9jl72l83w4f14xxxx",
    meeting_id: str | None = "6911188411934433028",
    subscriber_open_ids: list[str] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {"minute_token": minute_token}
    if meeting_id is not None:
        event["minute_source"] = {
            "source_type": "meeting",
            "source_entity_id": meeting_id,
        }
    if subscriber_open_ids is not None:
        event["subscriber_ids"] = [
            {"open_id": oid} for oid in subscriber_open_ids
        ]
    return {
        "header": {
            "event_id": event_id,
            "event_type": EVENT_MINUTE_GENERATED,
        },
        "event": event,
    }


def _patch_subscriber_users(
    monkeypatch: pytest.MonkeyPatch,
    *,
    open_id_to_user_id: dict[str, int],
    token_user_ids: list[int],
) -> None:
    class StubUser:
        def __init__(self, user_id: int) -> None:
            self.id = user_id

    class StubUsers:
        async def get_by_feishu_open_id(self, open_id: str) -> StubUser | None:
            uid = open_id_to_user_id.get(open_id)
            return StubUser(uid) if uid is not None else None

    class StubTokens:
        async def list_user_ids_with_tokens(self) -> list[int]:
            return list(token_user_ids)

    class StubUow:
        def __init__(self) -> None:
            self.users = StubUsers()
            self.feishu_user_tokens = StubTokens()

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr("app.service.feishu_event_service.UnitOfWork", lambda: StubUow())


def test_minute_generated_triggers_download_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDownload()
    service = FeishuEventService(download=fake)  # pyright: ignore[reportArgumentType]
    _patch_subscriber_users(
        monkeypatch,
        open_id_to_user_id={"ou_owner": 42},
        token_user_ids=[42, 99],
    )

    result = asyncio.run(
        service.handle_event(_payload(subscriber_open_ids=["ou_owner"]))
    )

    assert result == {
        "handled": True,
        "event_type": EVENT_MINUTE_GENERATED,
        "record_ids": [7],
    }
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["minute_token"] == "obcnq3b9jl72l83w4f14xxxx"
    assert call["feishu_event_id"] == "evt-1"
    assert call["event_type"] == EVENT_MINUTE_GENERATED
    assert call["unique_key"] == "6911188411934433028"
    assert call["skip_if_completed"] is True
    assert call["ready_retries"] >= 1
    assert call["owner_user_id"] == 42


def test_minute_generated_skips_users_not_in_subscribers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDownload()
    service = FeishuEventService(download=fake)  # pyright: ignore[reportArgumentType]
    _patch_subscriber_users(
        monkeypatch,
        open_id_to_user_id={"ou_owner": 42},
        token_user_ids=[42, 4],
    )

    result = asyncio.run(
        service.handle_event(_payload(subscriber_open_ids=["ou_owner"]))
    )

    assert result is not None
    assert result["record_ids"] == [7]
    assert [c["owner_user_id"] for c in fake.calls] == [42]


def test_minute_generated_without_subscribers_skips_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDownload()
    service = FeishuEventService(download=fake)  # pyright: ignore[reportArgumentType]
    _patch_subscriber_users(
        monkeypatch,
        open_id_to_user_id={"ou_owner": 42},
        token_user_ids=[42],
    )

    result = asyncio.run(service.handle_event(_payload(subscriber_open_ids=[])))

    assert result == {
        "handled": True,
        "event_type": EVENT_MINUTE_GENERATED,
        "record_ids": [],
    }
    assert fake.calls == []


def test_minute_generated_without_token_is_ignored() -> None:
    fake = FakeDownload()
    service = FeishuEventService(download=fake)  # pyright: ignore[reportArgumentType]

    result = asyncio.run(
        service.handle_event(
            {
                "header": {"event_id": "evt-2", "event_type": EVENT_MINUTE_GENERATED},
                "event": {},
            }
        )
    )

    assert result == {
        "handled": True,
        "event_type": EVENT_MINUTE_GENERATED,
        "record_ids": [],
    }
    assert fake.calls == []


def test_unknown_event_type_is_ignored() -> None:
    fake = FakeDownload()
    service = FeishuEventService(download=fake)  # pyright: ignore[reportArgumentType]

    result = asyncio.run(
        service.handle_event(
            {
                "header": {
                    "event_id": "evt-3",
                    "event_type": "vc.recording.recording_transcript_generated_v1",
                },
                "event": {},
            }
        )
    )

    assert result is None
    assert fake.calls == []


def _patch_download_uow(
    monkeypatch: pytest.MonkeyPatch, service: MeetingDownloadService
) -> None:
    class StubEntity:
        def __init__(self) -> None:
            self.id: int | None = None

    class StubRecords:
        async def get_by_event_id(self, _event_id: str) -> None:
            return None

        async def get_latest_by_minute_token(
            self, _token: str, *, owner_user_id: int
        ) -> None:
            return None

        async def create(self, _entity: Any) -> StubEntity:
            created = StubEntity()
            created.id = 1
            return created

        async def update(self, _entity: Any) -> None:
            return None

    class StubUow:
        def __init__(self) -> None:
            self.meeting_records = StubRecords()

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    monkeypatch.setattr("app.service.meeting_download_service.UnitOfWork", lambda: StubUow())


def test_download_retries_when_minute_not_ready(monkeypatch: Any) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    service = MeetingDownloadService()
    attempts = {"n": 0}

    async def flaky_store(*_args: Any, **_kwargs: Any) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError(
                "飞书 API 错误 [2091003]: minute not ready , try later (path=/x)"
            )

    monkeypatch.setattr(service, "_download_and_store", flaky_store)

    async def fake_enqueue(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(service, "enqueue_followups", fake_enqueue)
    _patch_download_uow(monkeypatch, service)

    result = asyncio.run(
        service.download_minute(
            "obcnq3b9jl72l83w4f14xxxx",
            feishu_event_id="evt-retry",
            ready_retries=4,
            ready_retry_seconds=1.5,
            owner_user_id=1,
        )
    )

    assert result.status == "COMPLETED"
    assert attempts["n"] == 3
    assert sleeps == [1.5, 1.5]


def test_download_does_not_retry_permission_errors(monkeypatch: Any) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    service = MeetingDownloadService()
    attempts = {"n": 0}

    async def always_denied(*_args: Any, **_kwargs: Any) -> None:
        attempts["n"] += 1
        raise RuntimeError("飞书 API 错误 [99991679]: Unauthorized (path=/x)")

    monkeypatch.setattr(service, "_download_and_store", always_denied)
    _patch_download_uow(monkeypatch, service)

    result = asyncio.run(
        service.download_minute(
            "obcnq3b9jl72l83w4f14xxxx",
            feishu_event_id="evt-denied",
            ready_retries=5,
            ready_retry_seconds=1.0,
            owner_user_id=1,
        )
    )

    assert result.status == "FAILED"
    assert result.error_code == 99991679
    assert attempts["n"] == 1
    assert sleeps == []
