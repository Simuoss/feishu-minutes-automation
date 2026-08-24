"""三个工具的行为：检索的与或与两种模式、read 的截断与上下统计、send 的卡片。"""

from __future__ import annotations

import asyncio
from typing import Any

from app.data_model.entity.agent_chat import SCOPE_OWN
from app.service.agent.tools import AgentToolbox, CardPayload
from app.service.agent.virtual_fs import MeetingSource, build_virtual_fs

TRANSCRIPT_A = "\n".join(
    [
        "2026-07-25 13:32:09 CST|41分钟 37秒",
        "",
        "周然 00:00:11.788 ",
        "我们先说环境变量怎么配。",
        "",
        "司沐 00:00:20.100 ",
        "包管理器装完之后再看路径问题。",
        "",
        "周然 00:01:02.000 ",
        "期末考试会考这个环境变量的题。",
    ]
)

TRANSCRIPT_B = "\n".join(
    [
        "2026-08-01 09:00:00 CST|20分钟",
        "",
        "李雷 00:00:05.000 ",
        "今天只讲包管理器，不讲别的。",
    ]
)

SUMMARY_A = "\n".join(
    [
        "# 课程纪要",
        "",
        "## 环境准备",
        "先装包管理器，再配环境变量，见 `00:00:20`。",
    ]
)

# 专门用来撞 limit 的长转写
TRANSCRIPT_LONG = "\n".join(
    ["2026-08-10 14:00:00 CST|90分钟", ""]
    + [
        line
        for i in range(40)
        for line in (
            f"讲师 00:{i:02d}:00.000 ",
            f"第 {i} 段在讲一个很长的知识点，这里要说满足够多的字数才能把上限撑开。",
            "",
        )
    ]
)


class FakeStorage:
    def __init__(self) -> None:
        self._transcripts = {
            "tokA": TRANSCRIPT_A,
            "tokB": TRANSCRIPT_B,
            "tokC": TRANSCRIPT_LONG,
        }
        self._summaries = {"tokA": SUMMARY_A}

    def has_transcript(self, minute_token: str, *, owner_user_id: int) -> bool:
        return minute_token in self._transcripts

    def has_summary(self, minute_token: str, *, owner_user_id: int) -> bool:
        return minute_token in self._summaries

    async def read_transcript_async(
        self, minute_token: str, *, owner_user_id: int, apply_names: bool = True
    ) -> str | None:
        return self._transcripts.get(minute_token)

    async def read_summary_async(
        self, minute_token: str, *, owner_user_id: int
    ) -> dict[str, Any] | None:
        content = self._summaries.get(minute_token)
        if content is None:
            return None
        return {"minute_token": minute_token, "content": content, "meta": {}}


PATH_A_TRANSCRIPT = "/2026-07-25 1332/环境搭建/转写.md"
PATH_A_SUMMARY = "/2026-07-25 1332/环境搭建/纪要.md"
PATH_B_TRANSCRIPT = "/2026-08-01 0900/包管理专场/转写.md"
PATH_C_TRANSCRIPT = "/2026-08-10 1400/长课/转写.md"


def _toolbox(**kwargs: Any) -> AgentToolbox:
    sources = [
        MeetingSource(
            minute_token="tokA",
            owner_user_id=1,
            title="环境搭建",
            create_time="2026-07-25 13:32:09",
        ),
        MeetingSource(
            minute_token="tokB",
            owner_user_id=1,
            title="包管理专场",
            create_time="2026-08-01 09:00:00",
        ),
        MeetingSource(
            minute_token="tokC",
            owner_user_id=1,
            title="长课",
            create_time="2026-08-10 14:00:00",
        ),
    ]
    vfs = asyncio.run(build_virtual_fs(sources, storage=FakeStorage()))
    return AgentToolbox(vfs=vfs, scope=SCOPE_OWN, **kwargs)


def _text(result: dict[str, Any]) -> str:
    return str(result["content"][0]["text"])


def test_keyword_search_reports_line_numbers_and_context():
    box = _toolbox()
    out = asyncio.run(box.search({"patterns": ["环境变量"]}))

    text = _text(out)
    assert "命中 3 处" in text  # 转写两处 + 纪要一处
    assert PATH_A_TRANSCRIPT in text
    assert "第 4 行" in text
    assert "> 4| 我们先说环境变量怎么配。" in text
    assert "tab=transcript&seg=0" in text
    # 上下文各三行，段首那行要跟着出来
    assert "3| 周然 00:00:11.788" in text


def test_or_logic_spans_files_while_and_logic_requires_both_in_one_file():
    box = _toolbox()

    or_out = _text(
        asyncio.run(
            box.search({"patterns": ["环境变量", "包管理器"], "logic": "or"})
        )
    )
    assert PATH_A_TRANSCRIPT in or_out
    assert PATH_B_TRANSCRIPT in or_out

    and_out = _text(
        asyncio.run(
            box.search({"patterns": ["环境变量", "包管理器"], "logic": "and"})
        )
    )
    # B 只讲包管理器，没提环境变量，与逻辑下整个文件被排除
    assert PATH_B_TRANSCRIPT not in and_out
    assert PATH_A_TRANSCRIPT in and_out


def test_regex_mode_actually_treats_the_pattern_as_a_regex():
    box = _toolbox()

    plain = _text(asyncio.run(box.search({"patterns": ["环境.{0,2}变量"]})))
    assert "没有命中" in plain

    regex = _text(
        asyncio.run(box.search({"patterns": ["环境.{0,2}变量"], "mode": "regex"}))
    )
    assert "命中" in regex and PATH_A_TRANSCRIPT in regex


def test_a_broken_regex_comes_back_as_a_tool_error():
    box = _toolbox()
    out = asyncio.run(box.search({"patterns": ["[未闭合"], "mode": "regex"}))

    assert out.get("is_error") is True
    assert "正则表达式不合法" in _text(out)


def test_empty_patterns_is_rejected():
    box = _toolbox()
    out = asyncio.run(box.search({"patterns": []}))

    assert out.get("is_error") is True


def test_paths_narrows_the_search_to_one_meeting():
    box = _toolbox()
    out = _text(
        asyncio.run(
            box.search(
                {"patterns": ["包管理器"], "paths": ["/2026-08-01 0900/包管理专场"]}
            )
        )
    )

    assert PATH_B_TRANSCRIPT in out
    assert PATH_A_SUMMARY not in out
    assert "已扫 1 个" in out


def test_max_results_truncates_and_says_so():
    box = _toolbox()
    out = _text(asyncio.run(box.search({"patterns": ["环境变量"], "max_results": 1})))

    assert "只列出前 1 处" in out


def test_read_returns_numbered_lines_plus_what_is_left_above_and_below():
    box = _toolbox()
    out = _text(
        asyncio.run(box.read({"path": PATH_A_TRANSCRIPT, "start_line": 3, "line_count": 2}))
    )

    assert "本段 第 3-4 行，共 10 行" in out
    assert "上方还有 2 行" in out
    assert "下方还有 6 行" in out
    assert "3| 周然 00:00:11.788" in out
    assert "4| 我们先说环境变量怎么配。" in out
    assert "5|" not in out


def test_read_truncates_at_the_char_limit_and_tells_where_to_resume():
    box = _toolbox()
    out = _text(
        asyncio.run(
            box.read(
                {
                    "path": PATH_C_TRANSCRIPT,
                    "start_line": 1,
                    "line_count": 200,
                    "limit": 300,
                }
            )
        )
    )

    assert "已达 limit=300 字" in out
    assert "start_line 设成" in out
    assert "下方还有" in out


def test_read_clamps_a_start_line_past_the_end_instead_of_blowing_up():
    box = _toolbox()
    out = _text(asyncio.run(box.read({"path": PATH_A_TRANSCRIPT, "start_line": 9999})))

    assert "共 10 行" in out


def test_read_rejects_a_path_outside_the_scope():
    box = _toolbox()
    out = asyncio.run(box.read({"path": "/别人的课/转写.md"}))

    assert out.get("is_error") is True
    assert "不在可检索范围内" in _text(out)


def test_send_hands_the_user_a_card_with_quote_and_link():
    got: list[CardPayload] = []

    async def sink(card: CardPayload) -> None:
        got.append(card)

    box = _toolbox(on_card=sink)
    out = asyncio.run(
        box.send(
            {
                "path": PATH_A_TRANSCRIPT,
                "start_line": 9,
                "line_count": 2,
                "note": "期末考点在这",
            }
        )
    )

    assert out.get("is_error") is None
    assert "已经把" in _text(out)
    assert len(got) == 1
    card = got[0]
    assert card.path == PATH_A_TRANSCRIPT
    assert card.start_line == 9 and card.end_line == 10
    assert "期末考试会考这个环境变量的题。" in card.quote
    assert card.url == "/meeting.html?token=tokA&tab=transcript&seg=2&segs=1"
    assert card.note == "期末考点在这"
    assert box.cards == got


def test_send_refuses_a_blank_span():
    box = _toolbox()
    out = asyncio.run(
        box.send({"path": PATH_A_TRANSCRIPT, "start_line": 2, "line_count": 1})
    )

    assert out.get("is_error") is True
    assert "空白" in _text(out)


def test_tool_events_fire_with_readable_briefs():
    started: list[tuple[str, str]] = []
    ended: list[tuple[str, str]] = []

    async def on_start(name: str, brief: str) -> None:
        started.append((name, brief))

    async def on_end(name: str, summary: str) -> None:
        ended.append((name, summary))

    box = _toolbox(on_tool_start=on_start, on_tool_end=on_end)
    asyncio.run(box.search({"patterns": ["包管理器"], "mode": "regex", "logic": "and"}))

    assert started[0][0] == "search"
    assert "包管理器" in started[0][1]
    assert "正则" in started[0][1] and "与" in started[0][1]
    assert ended[0][0] == "search"
    assert "命中" in ended[0][1]


def test_the_three_tools_are_exposed_to_the_sdk_with_schemas():
    box = _toolbox()
    tools = box.sdk_tools()

    assert [t.name for t in tools] == ["search", "read", "send"]
    search_schema = tools[0].input_schema
    assert search_schema["properties"]["patterns"]["type"] == "array"
    assert search_schema["required"] == ["patterns"]
