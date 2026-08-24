"""检索助手的编排：建会话、跑一轮问答、把 SDK 的消息翻成 SSE 事件。

上下文交给 Claude Agent SDK 保管：我们自己生成 UUID 当 session_id，之后每轮
用 resume 续上，所以库里只存展示用的消息，不负责回放。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    create_sdk_mcp_server,
)
from claude_agent_sdk.types import StreamEvent

from app.core import runtime_config
from app.core.config import settings
from app.data_model.entity.agent_chat import (
    ROLE_ASSISTANT,
    ROLE_CARD,
    ROLE_TOOL,
    ROLE_USER,
    SCOPE_ALL,
    SCOPE_OWN,
    SCOPE_SHARE,
    AgentMessageCreateEntity,
    AgentSessionCreateEntity,
    AgentSessionEntity,
)
from app.repository.uow import UnitOfWork
from app.service.agent import quota_service
from app.service.agent.quota_service import Subject
from app.service.agent.session_pool import agent_session_pool
from app.service.agent.stream_events import (
    delta_piece,
    opened_block,
    started_tool,
    thinking_from_assistant,
)
from app.service.agent.tools import AgentToolbox, CardPayload
from app.service.agent.virtual_fs import (
    MeetingSource,
    VirtualFs,
    build_virtual_fs,
    collect_owner_sources,
)
from app.service.time_utils import utc_now_ms

logger = logging.getLogger(__name__)

# CLI 会把会话记录写在 cwd 对应的目录下，单独给它一个空目录，别混进代码库
SESSION_WORKDIR = Path(settings.storage_root).parent / "agent_sessions"

BLOCKED_BUILTINS = [
    "Bash",
    "BashOutput",
    "CronCreate",
    "CronDelete",
    "CronList",
    "Edit",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "Glob",
    "Grep",
    "KillShell",
    "NotebookEdit",
    "Read",
    "ReportFindings",
    "ScheduleWakeup",
    "SendMessage",
    "Skill",
    "SlashCommand",
    "Task",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
]

MCP_SERVER_NAME = "minutes"
ALLOWED_TOOLS = [
    f"mcp__{MCP_SERVER_NAME}__search",
    f"mcp__{MCP_SERVER_NAME}__read",
    f"mcp__{MCP_SERVER_NAME}__send",
]

SYSTEM_PROMPT_TEMPLATE = """你是课程资料检索助手，帮用户在他有权访问的课程转写和纪要里找东西。

你只有三个工具，除此之外什么都做不了：
- search：按关键字或正则在资料里检索，返回命中的路径、行号和上下文。
- read：读某个文件的某一段，按行取。
- send：把一段原文连同跳转链接推给用户，在对话里显示成卡片。

规矩：
1. 凡是涉及资料内容的问题，先检索再回答，不许凭印象编。检索不到就直说没找到。
2. 路径只能用下面清单里的，一字不差地照抄。不要猜路径，也不要想象清单外的文件。
3. 找到关键段落就用 send 发给用户，让他能点过去看原文；发过之后回答里不必再抄一遍长引文。
4. 回答用中文，简明扼要，说清结论在哪场课的哪个位置。
5. 涉及多场课时，按时间顺序说，并说明每条结论出自哪一场。

当前可检索的资料清单（共 {file_count} 个文件）：
{tree}
"""


@dataclass
class AgentScope:
    """一次会话的检索范围。"""

    scope: str
    owner_user_id: int | None = None
    share_sources: list[MeetingSource] = field(default_factory=list)

    async def sources(self) -> list[MeetingSource]:
        if self.scope == SCOPE_SHARE:
            return list(self.share_sources)
        return await collect_owner_sources(self.owner_user_id)


class AgentDisabled(Exception):
    pass


class AgentSessionNotFound(Exception):
    pass


def agent_enabled() -> bool:
    return runtime_config.get_bool("AGENT_ENABLED", settings.agent_enabled)


def _require_enabled() -> None:
    if not agent_enabled():
        raise AgentDisabled("检索助手当前未开启")


def _max_turns() -> int:
    return max(1, runtime_config.get_int("AGENT_MAX_TURNS", settings.agent_max_turns))


def _search_max_results() -> int:
    return max(
        1,
        runtime_config.get_int(
            "AGENT_SEARCH_MAX_RESULTS", settings.agent_search_max_results
        ),
    )


async def create_session(*, subject: Subject, scope: AgentScope) -> AgentSessionEntity:
    _require_enabled()
    now = utc_now_ms()
    async with UnitOfWork() as uow:
        assert uow.agent_sessions is not None
        session = await uow.agent_sessions.create(
            AgentSessionCreateEntity(
                sdk_session_id=str(uuid.uuid4()),
                subject_type=subject.type,
                subject_key=subject.key,
                owner_user_id=scope.owner_user_id,
                scope=scope.scope,
                created_at=now,
                updated_at=now,
            )
        )
        await uow.commit()
    return session


async def list_sessions(subject: Subject, *, limit: int = 50) -> list[AgentSessionEntity]:
    async with UnitOfWork() as uow:
        assert uow.agent_sessions is not None
        return await uow.agent_sessions.list_by_subject(
            subject.type, subject.key, limit=limit
        )


async def _load_owned_session(
    session_id: int, subject: Subject
) -> AgentSessionEntity:
    async with UnitOfWork() as uow:
        assert uow.agent_sessions is not None
        session = await uow.agent_sessions.get(session_id)
    if (
        session is None
        or session.subject_type != subject.type
        or session.subject_key != subject.key
    ):
        raise AgentSessionNotFound("对话不存在或不属于你")
    return session


async def assert_owned(session_id: int, subject: Subject) -> None:
    """路由层先验一遍归属，好让不存在的会话返回 404 而不是一条坏掉的 SSE。"""
    await _load_owned_session(session_id, subject)


async def list_messages(session_id: int, subject: Subject) -> list[dict[str, Any]]:
    await _load_owned_session(session_id, subject)
    async with UnitOfWork() as uow:
        assert uow.agent_messages is not None
        messages = await uow.agent_messages.list_by_session(session_id)
    return [
        {
            "seq": m.seq,
            "role": m.role,
            "content": m.content,
            "meta": m.meta,
            "created_at": m.created_at,
        }
        for m in messages
    ]


async def _append_message(
    session_id: int, role: str, content: str, meta: dict[str, Any] | None = None
) -> None:
    async with UnitOfWork() as uow:
        assert uow.agent_messages is not None
        await uow.agent_messages.append(
            AgentMessageCreateEntity(
                session_id=session_id,
                role=role,
                content=content,
                created_at=utc_now_ms(),
                meta=meta or {},
            )
        )
        await uow.commit()


def _build_options(
    session: AgentSessionEntity, toolbox: AgentToolbox, vfs: VirtualFs
) -> ClaudeAgentOptions:
    SESSION_WORKDIR.mkdir(parents=True, exist_ok=True)
    endpoint = settings.agent_llm
    server = create_sdk_mcp_server(
        name=MCP_SERVER_NAME, version="1.0.0", tools=toolbox.sdk_tools()
    )
    options = ClaudeAgentOptions(
        mcp_servers={MCP_SERVER_NAME: server},
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=BLOCKED_BUILTINS,
        # 不读本机 .claude/ 与 CLAUDE.md，也不认项目里的 .mcp.json
        setting_sources=[],
        strict_mcp_config=True,
        include_partial_messages=True,
        # 打开思考并把正文吐出来；不设的话兼容网关经常只给签名，前端中间那段就是空白
        thinking={"type": "enabled", "budget_tokens": 8192, "display": "summarized"},
        permission_mode="bypassPermissions",
        max_turns=_max_turns(),
        cwd=str(SESSION_WORKDIR),
        system_prompt=SYSTEM_PROMPT_TEMPLATE.format(
            file_count=len(vfs), tree=vfs.tree_listing()
        ),
        env={
            "ANTHROPIC_BASE_URL": endpoint.base_url,
            "ANTHROPIC_AUTH_TOKEN": endpoint.api_key,
            "ANTHROPIC_API_KEY": endpoint.api_key,
            "ANTHROPIC_MODEL": endpoint.model,
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "128000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
        stderr=lambda line: logger.debug("[agent-cli] %s", line),
    )
    # 首轮用我们生成的 id 建会话，之后每轮 resume 同一个 id 续上下文
    if session.turn_count <= 0:
        options.session_id = session.sdk_session_id
    else:
        options.resume = session.sdk_session_id
    return options


async def iter_chat_events(
    session_id: int,
    question: str,
    *,
    subject: Subject,
    scope: AgentScope,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """跑一轮问答，边跑边吐 SSE 事件。"""
    _require_enabled()
    try:
        session = await _load_owned_session(session_id, subject)
    except AgentSessionNotFound as exc:
        # 路由已经先验过一遍，走到这里说明会话中途没了；SSE 里报错比抛出去好收场
        yield ("error", {"detail": str(exc)})
        return
    text = (question or "").strip()
    if not text:
        yield ("error", {"detail": "问题不能为空"})
        return

    try:
        quota = await quota_service.consume(subject)
    except quota_service.AgentQuotaExceeded as exc:
        yield ("error", {"detail": str(exc), "quota_exceeded": True})
        return

    await _append_message(session_id, ROLE_USER, text)
    async with UnitOfWork() as uow:
        assert uow.agent_sessions is not None
        await uow.agent_sessions.bump_turn(
            session_id, now_ms=utc_now_ms(), title=text[:60]
        )
        await uow.commit()

    started = time.perf_counter()

    def elapsed() -> float:
        return round(time.perf_counter() - started, 2)

    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    async def push(event: str, data: dict[str, Any]) -> None:
        await queue.put((event, data))

    cards: list[CardPayload] = []
    # 思考和工具痕迹按发生顺序落库，回看对话时中间那段空白才填得上
    pending_thinking: list[str] = []
    last_tool_brief: dict[str, str] = {}
    streamed_thinking = False

    async def persist(role: str, content: str, meta: dict[str, Any] | None = None) -> None:
        await _append_message(session_id, role, content, meta=meta)

    async def flush_thinking() -> None:
        text = "".join(pending_thinking).strip()
        pending_thinking.clear()
        if text:
            await persist(ROLE_TOOL, text, {"kind": "thinking"})

    async def on_card(card: CardPayload) -> None:
        cards.append(card)
        await persist(ROLE_CARD, card.quote, card.to_dict())
        await push("card", card.to_dict())

    async def on_tool_start(name: str, brief: str) -> None:
        last_tool_brief[name] = brief
        await push(
            "tool_start",
            {"name": name, "brief": brief, "elapsed_seconds": elapsed()},
        )

    async def on_tool_end(name: str, summary: str) -> None:
        await flush_thinking()
        await persist(
            ROLE_TOOL,
            summary,
            {
                "kind": "trace",
                "name": name,
                "brief": last_tool_brief.get(name, ""),
                "summary": summary,
            },
        )
        await push(
            "tool_end", {"name": name, "summary": summary, "elapsed_seconds": elapsed()}
        )

    async def run() -> None:
        nonlocal streamed_thinking
        reply = ""
        try:
            sources = await scope.sources()
            vfs = await build_virtual_fs(sources)
            await push(
                "status",
                {
                    "stage": "READY",
                    "file_count": len(vfs),
                    "elapsed_seconds": elapsed(),
                },
            )
            if not len(vfs):
                await push(
                    "error",
                    {"detail": "这个范围内还没有任何可检索的课程资料"},
                )
                return

            toolbox = AgentToolbox(
                vfs=vfs,
                scope=session.scope,
                max_results=_search_max_results(),
                on_card=on_card,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
            )
            options = _build_options(session, toolbox, vfs)

            async with agent_session_pool.slot() as waited:
                if waited:
                    await push(
                        "status",
                        {"stage": "QUEUED", "elapsed_seconds": elapsed()},
                    )
                await push(
                    "status", {"stage": "WAITING_MODEL", "elapsed_seconds": elapsed()}
                )
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(text)
                    async for message in client.receive_response():
                        if isinstance(message, StreamEvent):
                            event = message.event or {}
                            opened = opened_block(event)
                            if opened == "thinking":
                                await push(
                                    "segment",
                                    {"kind": "thinking", "elapsed_seconds": elapsed()},
                                )
                            elif opened == "text":
                                await flush_thinking()
                                await push(
                                    "segment",
                                    {"kind": "text", "elapsed_seconds": elapsed()},
                                )
                            tool_name = started_tool(event)
                            if tool_name is not None:
                                await flush_thinking()
                                # 入参还在流，先占位，等工具真正跑起来再换成带检索词的摘要
                                last_tool_brief.setdefault(tool_name, "准备调用…")
                                await push(
                                    "tool_start",
                                    {
                                        "name": tool_name,
                                        "brief": last_tool_brief[tool_name],
                                        "elapsed_seconds": elapsed(),
                                    },
                                )
                            piece = delta_piece(event)
                            if piece is not None:
                                kind, chunk = piece
                                if kind == "thinking":
                                    streamed_thinking = True
                                    pending_thinking.append(chunk)
                                    await push("thinking", {"text": chunk})
                                else:
                                    await push("delta", {"text": chunk})
                        elif isinstance(message, AssistantMessage):
                            # 兼容网关不推 thinking_delta 时，整段消息里可能还带着思考
                            if not streamed_thinking:
                                fallback = thinking_from_assistant(message)
                                if fallback:
                                    pending_thinking.append(fallback)
                                    await push(
                                        "segment",
                                        {
                                            "kind": "thinking",
                                            "elapsed_seconds": elapsed(),
                                        },
                                    )
                                    await push("thinking", {"text": fallback})
                            streamed_thinking = False
                        elif isinstance(message, ResultMessage):
                            reply = str(message.result or "").strip()
                            if message.is_error and not reply:
                                await push(
                                    "error",
                                    {"detail": f"模型调用失败（{message.subtype}）"},
                                )
                                return
                            usage = message.usage or {}
                            await push(
                                "done",
                                {
                                    "reply": reply,
                                    "cards": [c.to_dict() for c in cards],
                                    "input_tokens": usage.get("input_tokens"),
                                    "output_tokens": usage.get("output_tokens"),
                                    "elapsed_seconds": elapsed(),
                                    "quota_remaining": quota.remaining,
                                },
                            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "检索助手这一轮跑挂了 session=%s：%s，怀疑上游或 CLI 子进程异常",
                session_id,
                exc,
                exc_info=True,
            )
            await push("error", {"detail": "提问失败，请稍后重试"})
        finally:
            await flush_thinking()
            if reply:
                await persist(ROLE_ASSISTANT, reply)
            await queue.put(None)

    task = asyncio.create_task(run())
    yield ("status", {"stage": "PREPARING", "elapsed_seconds": 0})
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    finally:
        await task


def scope_for_user(*, user_id: int | None, is_super_admin: bool) -> AgentScope:
    """超管默认全站，普通用户只看自己名下。"""
    if is_super_admin:
        return AgentScope(scope=SCOPE_ALL, owner_user_id=None)
    if user_id is None:
        raise AgentSessionNotFound("需要登录")
    return AgentScope(scope=SCOPE_OWN, owner_user_id=user_id)


def scope_for_share(sources: list[MeetingSource]) -> AgentScope:
    return AgentScope(scope=SCOPE_SHARE, owner_user_id=None, share_sources=sources)
