"""会议主档只有一个写入者。

以前下载完会先按一份 meta 字典写一遍 meeting_records，再按字段类型化地写第二遍，
两遍的合并规则还不一样：字典那遍靠 `{**existing_meta}` 保留旧值，类型化那遍靠仓储
跳过 None。这里盯的是合成一条路之后，「飞书这次没给的字段」仍然保得住。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.data_model.entity.meeting_record import MeetingRecordCreateEntity
from app.integrations.feishu.minutes_client import MinuteInfo
from app.repository.uow import UnitOfWork
from app.service.meeting_download_service import MeetingDownloadService
from app.service.meeting_storage_service import MeetingStorageService

OWNER = 11
TOKEN = "minutetoken001"


class _FakeMinutes:
    """只回答下载流程要问的那几个问题。"""

    def __init__(self, *, create_time: str | None) -> None:
        self._create_time = create_time
        self._api = object()

    async def get_minute_info(self, token: str, **_kwargs: object) -> MinuteInfo:
        return MinuteInfo(
            token=token,
            title="第二次拉的标题",
            create_time=self._create_time,
            duration_ms=60_000,
            url=None,
            note_id=None,
            cover=None,
            owner_id=None,
        )

    async def download_transcript(self, token: str, **_kwargs: object) -> bytes:
        return "张三 00:00:01.000\n开会了。\n".encode("utf-8")


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage = MeetingStorageService(storage_root=str(tmp_path / "meetings"))
    svc = MeetingDownloadService(storage=storage)
    return svc, storage


def _seed_record(*, media_relpath: str, create_time: str, unique_key: str) -> int:
    async def run() -> int:
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            record = await uow.meeting_records.create(
                MeetingRecordCreateEntity(
                    feishu_event_id=f"seed:{TOKEN}",
                    event_type="MANUAL",
                    minute_token=TOKEN,
                    unique_key=unique_key,
                    title="第一次拉的标题",
                    has_video=True,
                    status="COMPLETED",
                    owner_user_id=OWNER,
                    media_relpath=media_relpath,
                    create_time=create_time,
                )
            )
            await uow.commit()
            assert record.id is not None
            return record.id

    return asyncio.run(run())


def _load():
    async def run():
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            return await uow.meeting_records.get_latest_by_minute_token(
                TOKEN, owner_user_id=OWNER
            )

    return asyncio.run(run())


@pytest.mark.usefixtures("_memory_db")
def test_transcript_only_redownload_keeps_the_media_it_did_not_touch(
    service, monkeypatch: pytest.MonkeyPatch
):
    svc, _ = service
    record_id = _seed_record(
        media_relpath="raw/media/会议.mp4",
        create_time="1700000000000",
        unique_key="uk-1",
    )
    monkeypatch.setattr(
        svc, "_minutes_for", lambda _owner: _FakeMinutes(create_time=None)
    )

    asyncio.run(
        svc._download_and_store(
            record_id,
            TOKEN,
            None,
            f"seed:{TOKEN}",
            fetch_media=False,
            fetch_transcript=True,
            owner_user_id=OWNER,
        )
    )

    record = _load()
    # 这一趟没碰媒体，原来的路径和「有视频」都得留着
    assert record.media_relpath == "raw/media/会议.mp4"
    assert record.has_video is True
    # 飞书这次没给生成时间，不能把列表的排序依据抹掉
    assert record.create_time == "1700000000000"
    # 调用方没带 unique_key，也不该当成置空
    assert record.unique_key == "uk-1"
    # 这一趟真拉到的东西照常更新
    assert record.title == "第二次拉的标题"
    assert record.transcript_relpath is not None
    assert record.downloaded_at is not None
    assert "张三" in (record.speakers_json or "")
