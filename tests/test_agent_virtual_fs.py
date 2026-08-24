"""虚拟目录：路径怎么长出来的、能不能反解、越权路径会不会被挡。"""

from __future__ import annotations

import asyncio
from typing import Any

from app.data_model.entity.agent_chat import SCOPE_ALL, SCOPE_OWN
from app.service.agent import link_builder
from app.service.agent.virtual_fs import (
    KIND_SUMMARY,
    KIND_TRANSCRIPT,
    MeetingSource,
    build_virtual_fs,
)

TRANSCRIPT = "\n".join(
    [
        "2026-07-25 13:32:09 CST|41分钟 37秒",
        "",
        "关键词:",
        "指令、官网、环境变量",
        "",
        "周然 00:00:11.788 ",
        "哦，太离谱。",
        "",
        "司沐 00:00:14.908 ",
        "你之前装过 Java 的包吗？要先配环境变量。",
    ]
)

SUMMARY = "\n".join(
    [
        "# 课程纪要",
        "",
        "## 环境准备",
        "先装包管理器，再配环境变量，见 `00:00:14`。",
    ]
)


class FakeStorage:
    """只认预置的几场会议，其它一律当作盘上没有。"""

    def __init__(self, present: dict[tuple[int, str], tuple[bool, bool]]) -> None:
        self._present = present

    def has_transcript(self, minute_token: str, *, owner_user_id: int) -> bool:
        return self._present.get((owner_user_id, minute_token), (False, False))[0]

    def has_summary(self, minute_token: str, *, owner_user_id: int) -> bool:
        return self._present.get((owner_user_id, minute_token), (False, False))[1]

    async def read_transcript_async(
        self, minute_token: str, *, owner_user_id: int, apply_names: bool = True
    ) -> str | None:
        return TRANSCRIPT

    async def read_summary_async(
        self, minute_token: str, *, owner_user_id: int
    ) -> dict[str, Any] | None:
        return {"minute_token": minute_token, "content": SUMMARY, "meta": {}}


def _storage(*tokens: str, summary: bool = True) -> FakeStorage:
    return FakeStorage({(1, token): (True, summary) for token in tokens})


def test_path_carries_the_meeting_time_and_name():
    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="项目周会",
                    create_time="2026-07-25 13:32:09",
                )
            ],
            storage=_storage("tok1"),
        )
    )

    paths = sorted(vf.path for vf in vfs.files)
    assert paths == [
        "/2026-07-25 1332/项目周会/纪要.md",
        "/2026-07-25 1332/项目周会/转写.md",
    ]


def test_an_epoch_create_time_is_labelled_in_beijing_time():
    """库里飞书的 create_time 存的是毫秒时间戳字符串，不是 ISO。"""
    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="晚课",
                    create_time="1786449628826",  # 2026-08-11 20:00 +08
                    downloaded_at=1786454490293,  # 21:21，下载时间不该当成上课时间
                )
            ],
            storage=_storage("tok1"),
        )
    )

    assert all(vf.path.startswith("/2026-08-11 2000/") for vf in vfs.files)


def test_the_download_time_only_fills_in_when_there_is_no_create_time():
    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="晚课",
                    downloaded_at=1786454490293,
                )
            ],
            storage=_storage("tok1"),
        )
    )

    assert all(vf.path.startswith("/2026-08-11 2121/") for vf in vfs.files)


def test_a_meeting_with_no_time_at_all_still_gets_a_directory():
    vfs = asyncio.run(
        build_virtual_fs(
            [MeetingSource(minute_token="tok1", owner_user_id=1, title="来历不明")],
            storage=_storage("tok1"),
        )
    )

    assert all(vf.path.startswith("/未知时间/") for vf in vfs.files)


def test_a_meeting_without_summary_only_exposes_the_transcript():
    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="项目周会",
                    create_time="2026-07-25 13:32:09",
                )
            ],
            storage=_storage("tok1", summary=False),
        )
    )

    assert [vf.kind for vf in vfs.files] == [KIND_TRANSCRIPT]


def test_meetings_with_nothing_on_disk_are_left_out():
    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(minute_token="ghost", owner_user_id=1, title="幽灵会议"),
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="项目周会",
                    create_time="2026-07-25 13:32:09",
                ),
            ],
            storage=_storage("tok1"),
        )
    )

    assert all("幽灵会议" not in vf.path for vf in vfs.files)
    assert len(vfs) == 2


def test_same_minute_same_name_gets_a_suffix_instead_of_colliding():
    sources = [
        MeetingSource(
            minute_token="tok1",
            owner_user_id=1,
            title="项目周会",
            create_time="2026-07-25 13:32:09",
        ),
        MeetingSource(
            minute_token="tok2",
            owner_user_id=1,
            title="项目周会",
            create_time="2026-07-25 13:32:40",
        ),
    ]
    vfs = asyncio.run(build_virtual_fs(sources, storage=_storage("tok1", "tok2")))

    dirs = {vf.path.rsplit("/", 1)[0] for vf in vfs.files}
    assert dirs == {"/2026-07-25 1332/项目周会", "/2026-07-25 1332/项目周会 (2)"}
    assert len(vfs) == 4


def test_a_slash_in_the_title_does_not_split_the_tree():
    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="AI/ML 入门",
                    create_time="2026-07-25 13:32:09",
                )
            ],
            storage=_storage("tok1"),
        )
    )

    for vf in vfs.files:
        assert vf.path.count("/") == 3
        assert "AI_ML 入门" in vf.path


def test_resolve_refuses_paths_outside_the_tree():
    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="项目周会",
                    create_time="2026-07-25 13:32:09",
                )
            ],
            storage=_storage("tok1"),
        )
    )

    assert vfs.resolve("/2026-07-25 1332/项目周会/转写.md") is not None
    assert vfs.resolve("/别人的会议/转写.md") is None
    assert vfs.resolve("../../../etc/passwd") is None
    assert vfs.resolve("/data/meetings/1/tok1/raw/transcript/transcript.txt") is None


def test_resolve_tolerates_the_model_dropping_the_extension():
    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="项目周会",
                    create_time="2026-07-25 13:32:09",
                )
            ],
            storage=_storage("tok1"),
        )
    )

    hit = vfs.resolve("2026-07-25 1332/项目周会/转写")
    assert hit is not None
    assert hit.kind == KIND_TRANSCRIPT


def test_lines_are_read_once_and_reused():
    calls = {"n": 0}

    class CountingStorage(FakeStorage):
        async def read_transcript_async(self, *args: Any, **kwargs: Any) -> str:
            calls["n"] += 1
            return TRANSCRIPT

    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="项目周会",
                    create_time="2026-07-25 13:32:09",
                )
            ],
            storage=CountingStorage({(1, "tok1"): (True, True)}),
        )
    )
    vf = next(f for f in vfs.files if f.kind == KIND_TRANSCRIPT)

    async def scenario() -> None:
        await vfs.lines(vf)
        await vfs.lines(vf)

    asyncio.run(scenario())
    assert calls["n"] == 1


def test_transcript_lines_map_onto_the_segments_the_frontend_renders():
    lines = TRANSCRIPT.split("\n")
    owner = link_builder.segment_of_lines(lines)

    # 前 5 行是表头，不属于任何发言段
    assert owner[:5] == [None] * 5
    assert owner[5] == 0  # 周然 那一行
    assert owner[7] == 0  # 段内空行仍归上一段
    assert owner[8] == 1  # 司沐 那一行
    assert link_builder.segment_range(lines, 6, 10) == (0, 1)
    assert link_builder.segment_range(lines, 1, 4) is None


def test_transcript_link_points_at_the_segment_span():
    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="项目周会",
                    create_time="2026-07-25 13:32:09",
                )
            ],
            storage=_storage("tok1"),
        )
    )
    vf = next(f for f in vfs.files if f.kind == KIND_TRANSCRIPT)
    lines = asyncio.run(vfs.lines(vf))

    url = link_builder.build_link(vf, lines, 9, 10, scope=SCOPE_OWN)
    assert url == "/meeting.html?token=tok1&tab=transcript&seg=1&segs=1"

    super_url = link_builder.build_link(vf, lines, 9, 10, scope=SCOPE_ALL)
    assert "owner_user_id=1" in super_url


def test_summary_link_carries_a_quote_because_markdown_has_no_line_numbers():
    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="项目周会",
                    create_time="2026-07-25 13:32:09",
                )
            ],
            storage=_storage("tok1"),
        )
    )
    vf = next(f for f in vfs.files if f.kind == KIND_SUMMARY)
    lines = asyncio.run(vfs.lines(vf))

    url = link_builder.build_link(vf, lines, 4, 4, scope=SCOPE_OWN)
    assert url.startswith("/meeting.html?token=tok1&tab=summary&q=")
    assert "%E7%8E%AF%E5%A2%83%E5%8F%98%E9%87%8F" in url  # 环境变量


def test_the_quote_never_straddles_a_markdown_marker():
    lines = [
        "- **`.env`** `00:50:56` — 保存环境变量的文件，通常用来放 API key。",
    ]

    hint = link_builder.quote_hint(lines, 1, 1)

    # 渲染后 .env 和时间各自成了独立元素，引文只能取那段连续的纯文本
    assert hint == "— 保存环境变量的文件，通常用来放 API key。"
    for marker in "`*[]()#>|":
        assert marker not in hint


def test_a_line_that_is_all_markup_yields_no_quote():
    assert link_builder.quote_hint(["## **`x`**"], 1, 1) == ""


def test_share_link_uses_the_share_token():
    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="项目周会",
                    create_time="2026-07-25 13:32:09",
                    share_token="sh_abc",
                )
            ],
            storage=_storage("tok1"),
        )
    )
    vf = next(f for f in vfs.files if f.kind == KIND_TRANSCRIPT)
    lines = asyncio.run(vfs.lines(vf))

    url = link_builder.build_link(vf, lines, 9, 10, scope="SHARE")
    assert url.startswith("/share.html?s=sh_abc&tab=transcript")


def test_tree_listing_groups_files_by_meeting():
    vfs = asyncio.run(
        build_virtual_fs(
            [
                MeetingSource(
                    minute_token="tok1",
                    owner_user_id=1,
                    title="项目周会",
                    create_time="2026-07-25 13:32:09",
                )
            ],
            storage=_storage("tok1"),
        )
    )

    listing = vfs.tree_listing()
    assert listing == "/2026-07-25 1332/项目周会/ -> 纪要.md、转写.md"
