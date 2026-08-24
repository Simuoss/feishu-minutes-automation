"""把 Claude Agent SDK 的流式事件翻成前端能画的一小段信息。

模型开口到工具真正跑起来中间有一段空白：先想、再吐工具入参、最后才进我们的
search/read/send。这段不推出去，用户就会对着「正在等模型开口」干等。
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk.types import ThinkingBlock

# 模型的思考块；redacted 那种正文是密文，只当成「在想」用来占位
_THINKING_BLOCKS = {"thinking", "redacted_thinking"}

# SDK 给 MCP 工具起的名字是 mcp__<server>__<tool>，前端只认短名
_MCP_PREFIX = "mcp__"


def short_tool_name(raw: str) -> str:
    name = (raw or "").strip()
    if name.startswith(_MCP_PREFIX):
        parts = name.split("__")
        if len(parts) >= 3:
            return parts[-1]
    return name or "tool"


def opened_block(event: dict[str, Any]) -> str | None:
    """新开了一个内容块的话，返回它是 thinking / text / tool。

    前端靠这个把一轮里的「想一下 → 查一次 → 再说一句」拆成前后独立的块。
    """
    if event.get("type") != "content_block_start":
        return None
    block_type = str((event.get("content_block") or {}).get("type") or "")
    if block_type in _THINKING_BLOCKS:
        return "thinking"
    if block_type == "text":
        return "text"
    if block_type == "tool_use":
        return "tool"
    return None


def started_tool(event: dict[str, Any]) -> str | None:
    """模型刚决定调用某个工具时返回短名，这时入参可能还没吐完。"""
    if opened_block(event) != "tool":
        return None
    raw = str((event.get("content_block") or {}).get("name") or "")
    return short_tool_name(raw)


def delta_piece(event: dict[str, Any]) -> tuple[str, str] | None:
    """把增量拆成 (thinking|text, 文本)；其它增量（工具入参、签名）不关心。"""
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta") or {}
    delta_type = str(delta.get("type") or "")
    if delta_type == "text_delta":
        text = str(delta.get("text") or "")
        return ("text", text) if text else None
    if delta_type == "thinking_delta":
        # 官方字段是 thinking，个别兼容网关会塞进 text，两个都认
        text = str(delta.get("thinking") or delta.get("text") or "")
        return ("thinking", text) if text else None
    return None


def thinking_from_assistant(message: Any) -> str:
    """流式思考没过来时，从整段 AssistantMessage 里把思考抠出来当兜底。"""
    parts: list[str] = []
    for block in getattr(message, "content", None) or []:
        if isinstance(block, ThinkingBlock):
            text = str(block.thinking or "").strip()
            if text:
                parts.append(text)
            continue
        if isinstance(block, dict) and str(block.get("type") or "") in _THINKING_BLOCKS:
            text = str(block.get("thinking") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)
