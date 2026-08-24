"""把 SDK 流式事件翻成前端能画的一小段信息。"""

from types import SimpleNamespace

from claude_agent_sdk.types import ThinkingBlock

from app.service.agent.stream_events import (
    delta_piece,
    opened_block,
    short_tool_name,
    started_tool,
    thinking_from_assistant,
)


def test_mcp_tool_name_is_shortened_to_the_bare_name():
    assert short_tool_name("mcp__minutes__search") == "search"
    assert short_tool_name("read") == "read"


def test_a_thinking_block_opening_is_recognized():
    event = {
        "type": "content_block_start",
        "content_block": {"type": "thinking"},
    }
    assert opened_block(event) == "thinking"


def test_a_tool_use_block_opening_is_recognized_as_soon_as_the_model_decides():
    event = {
        "type": "content_block_start",
        "content_block": {"type": "tool_use", "name": "mcp__minutes__read", "id": "1"},
    }
    assert opened_block(event) == "tool"
    assert started_tool(event) == "read"


def test_thinking_delta_accepts_either_field_name():
    official = {
        "type": "content_block_delta",
        "delta": {"type": "thinking_delta", "thinking": "先查转写"},
    }
    compat = {
        "type": "content_block_delta",
        "delta": {"type": "thinking_delta", "text": "先查转写"},
    }
    assert delta_piece(official) == ("thinking", "先查转写")
    assert delta_piece(compat) == ("thinking", "先查转写")


def test_text_delta_is_kept_separate_from_thinking():
    event = {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "结论是"},
    }
    assert delta_piece(event) == ("text", "结论是")


def test_tool_argument_deltas_are_ignored():
    event = {
        "type": "content_block_delta",
        "delta": {"type": "input_json_delta", "partial_json": "{\"p"},
    }
    assert delta_piece(event) is None
    assert started_tool(event) is None


def test_thinking_can_be_pulled_out_of_a_finished_assistant_message():
    message = SimpleNamespace(
        content=[
            ThinkingBlock(thinking="要先 search 再 read", signature="sig"),
            SimpleNamespace(text="我去查一下"),
        ]
    )
    assert thinking_from_assistant(message) == "要先 search 再 read"
