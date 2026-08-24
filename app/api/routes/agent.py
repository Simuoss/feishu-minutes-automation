"""检索助手路由：管理端登录用户 + 分享访客。

访客那一侧挂在 /share/ 前缀下（该前缀免 JWT），所以鉴权靠请求体里的密钥与
已知分享 token 过一遍 resolve_library：算出来是空集就没权限，算出来的集合同时
就是这次会话的检索范围。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.auth_context import require_auth
from app.core.client_ip import client_ip
from app.data_model.entity.agent_chat import SUBJECT_USER
from app.dto.agent import (
    AgentChatRequest,
    AgentMessageData,
    AgentMessageListResponse,
    AgentSessionData,
    AgentSessionListResponse,
    AgentStatusResponse,
    GuestChatRequest,
    GuestScopeMixin,
    GuestSessionRequest,
)
from app.service.agent import agent_service, quota_service
from app.service.agent.agent_service import (
    AgentDisabled,
    AgentScope,
    AgentSessionNotFound,
)
from app.service.agent.quota_service import Subject
from app.service.agent.virtual_fs import MeetingSource
from app.service.share_service import share_service

router = APIRouter(tags=["agent"])


def _session_data(session) -> AgentSessionData:
    return AgentSessionData(
        id=int(session.id or 0),
        title=session.title,
        scope=session.scope,
        turn_count=session.turn_count,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _map_http(exc: Exception) -> HTTPException:
    if isinstance(exc, AgentDisabled):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, AgentSessionNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, quota_service.AgentQuotaExceeded):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="检索助手出错了")


def _sse(events: AsyncIterator[tuple[str, dict]]) -> StreamingResponse:
    async def event_source() -> AsyncIterator[str]:
        async for name, data in events:
            yield f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------- 登录侧 ----------


def _user_identity(request: Request) -> tuple[Subject, AgentScope]:
    auth = require_auth(request)
    if auth.is_super_admin:
        # 超管没有 user_id，用固定主体记账，检索范围是全站
        return Subject(
            type=SUBJECT_USER, key="super"
        ), agent_service.scope_for_user(user_id=None, is_super_admin=True)
    if auth.user_id is None:
        raise HTTPException(status_code=401, detail="需要登录")
    return Subject.for_user(auth.user_id), agent_service.scope_for_user(
        user_id=auth.user_id, is_super_admin=False
    )


@router.get("/agent/status", response_model=AgentStatusResponse)
async def agent_status(request: Request) -> AgentStatusResponse:
    subject, _ = _user_identity(request)
    state = await quota_service.peek(subject)
    return AgentStatusResponse(
        enabled=agent_service.agent_enabled(),
        quota_limit=state.limit,
        quota_used=state.used,
    )


@router.get("/agent/sessions", response_model=AgentSessionListResponse)
async def list_agent_sessions(request: Request) -> AgentSessionListResponse:
    subject, _ = _user_identity(request)
    sessions = await agent_service.list_sessions(subject)
    state = await quota_service.peek(subject)
    return AgentSessionListResponse(
        items=[_session_data(s) for s in sessions],
        quota_limit=state.limit,
        quota_used=state.used,
    )


@router.post("/agent/sessions", response_model=AgentSessionData)
async def create_agent_session(request: Request) -> AgentSessionData:
    subject, scope = _user_identity(request)
    try:
        session = await agent_service.create_session(subject=subject, scope=scope)
    except AgentDisabled as exc:
        raise _map_http(exc) from exc
    return _session_data(session)


@router.get(
    "/agent/sessions/{session_id}/messages", response_model=AgentMessageListResponse
)
async def list_agent_messages(
    session_id: int, request: Request
) -> AgentMessageListResponse:
    subject, _ = _user_identity(request)
    try:
        messages = await agent_service.list_messages(session_id, subject)
    except AgentSessionNotFound as exc:
        raise _map_http(exc) from exc
    return AgentMessageListResponse(
        items=[AgentMessageData(**m) for m in messages]
    )


@router.post("/agent/sessions/{session_id}/chat/stream")
async def chat_stream(
    session_id: int, body: AgentChatRequest, request: Request
) -> StreamingResponse:
    subject, scope = _user_identity(request)
    if not agent_service.agent_enabled():
        raise HTTPException(status_code=503, detail="检索助手当前未开启")
    try:
        await agent_service.assert_owned(session_id, subject)
    except AgentSessionNotFound as exc:
        raise _map_http(exc) from exc
    return _sse(
        agent_service.iter_chat_events(
            session_id, body.question, subject=subject, scope=scope
        )
    )


# ---------- 访客侧 ----------


async def _guest_identity(
    body: GuestScopeMixin, request: Request, share_session: str | None
) -> tuple[Subject, AgentScope]:
    """把本机密钥换成可检索范围；顺便定下配额主体。"""
    if not body.keys and not body.share_tokens:
        raise HTTPException(status_code=403, detail="没有可访问的课程")

    result = await share_service.resolve_library(
        keys=list(body.keys), share_tokens=list(body.share_tokens)
    )
    sources: list[MeetingSource] = []
    for item in result.items:
        if item.owner_user_id is None:
            continue
        sources.append(
            MeetingSource(
                minute_token=item.minute_token,
                owner_user_id=int(item.owner_user_id),
                title=item.title,
                create_time=item.create_time,
                share_token=item.share_token,
            )
        )
    if not sources:
        raise HTTPException(status_code=403, detail="没有可访问的课程")

    if result.usable_key_hashes:
        subject = Subject.for_access_keys(result.usable_key_hashes)
    else:
        # 手上没有有效密钥，只是知道几个公开分享，按公开访客限额
        subject = Subject.for_anonymous(share_session, client_ip(request))
    return subject, agent_service.scope_for_share(sources)


@router.post("/share/agent/sessions", response_model=AgentSessionData)
async def create_guest_session(
    body: GuestSessionRequest,
    request: Request,
    x_share_session: str | None = Header(default=None),
) -> AgentSessionData:
    subject, scope = await _guest_identity(body, request, x_share_session)
    try:
        session = await agent_service.create_session(subject=subject, scope=scope)
    except AgentDisabled as exc:
        raise _map_http(exc) from exc
    return _session_data(session)


@router.post("/share/agent/sessions/{session_id}/messages")
async def list_guest_messages(
    session_id: int,
    body: GuestSessionRequest,
    request: Request,
    x_share_session: str | None = Header(default=None),
) -> AgentMessageListResponse:
    """访客侧用 POST 取历史，因为身份要靠请求体里的密钥带上来。"""
    subject, _ = await _guest_identity(body, request, x_share_session)
    try:
        messages = await agent_service.list_messages(session_id, subject)
    except AgentSessionNotFound as exc:
        raise _map_http(exc) from exc
    return AgentMessageListResponse(items=[AgentMessageData(**m) for m in messages])


@router.post("/share/agent/sessions/list")
async def list_guest_sessions(
    body: GuestSessionRequest,
    request: Request,
    x_share_session: str | None = Header(default=None),
) -> AgentSessionListResponse:
    subject, _ = await _guest_identity(body, request, x_share_session)
    sessions = await agent_service.list_sessions(subject)
    state = await quota_service.peek(subject)
    return AgentSessionListResponse(
        items=[_session_data(s) for s in sessions],
        quota_limit=state.limit,
        quota_used=state.used,
    )


@router.post("/share/agent/sessions/{session_id}/chat/stream")
async def guest_chat_stream(
    session_id: int,
    body: GuestChatRequest,
    request: Request,
    x_share_session: str | None = Header(default=None),
) -> StreamingResponse:
    subject, scope = await _guest_identity(body, request, x_share_session)
    if not agent_service.agent_enabled():
        raise HTTPException(status_code=503, detail="检索助手当前未开启")
    try:
        await agent_service.assert_owned(session_id, subject)
    except AgentSessionNotFound as exc:
        raise _map_http(exc) from exc
    return _sse(
        agent_service.iter_chat_events(
            session_id, body.question, subject=subject, scope=scope
        )
    )


@router.post("/share/agent/status", response_model=AgentStatusResponse)
async def guest_agent_status(
    body: GuestSessionRequest,
    request: Request,
    x_share_session: str | None = Header(default=None),
) -> AgentStatusResponse:
    subject, scope = await _guest_identity(body, request, x_share_session)
    state = await quota_service.peek(subject)
    return AgentStatusResponse(
        enabled=agent_service.agent_enabled(),
        quota_limit=state.limit,
        quota_used=state.used,
        file_count=len(scope.share_sources),
    )
