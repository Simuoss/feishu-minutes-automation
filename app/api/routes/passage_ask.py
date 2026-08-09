"""划词答疑路由：管理端会议 + 访客分享。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.auth_context import require_user_id
from app.dto.passage_ask import PassageAskRequest, PassageAskResponse
from app.service.ownership import assert_meeting_readable
from app.service.passage_ask_service import passage_ask_service

router = APIRouter(tags=["passage-ask"])


def _to_history(body: PassageAskRequest) -> list[dict]:
    return [
        {
            "role": item.role,
            "content": item.content,
            "images": [
                {"media_type": img.media_type, "data_base64": img.data_base64}
                for img in item.images
            ],
        }
        for item in body.history
    ]


def _to_images(body: PassageAskRequest) -> list[dict]:
    return [
        {"media_type": img.media_type, "data_base64": img.data_base64}
        for img in body.images
    ]


def _map_http(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail="提问失败")


async def _run_ask(
    *,
    minute_token: str,
    owner_user_id: int,
    body: PassageAskRequest,
) -> PassageAskResponse:
    try:
        result = await passage_ask_service.ask(
            minute_token=minute_token,
            owner_user_id=owner_user_id,
            source_kind=body.source_kind,
            selected_text=body.selected_text,
            history=_to_history(body),
            question=body.question,
            images=_to_images(body),
        )
    except (LookupError, PermissionError, ValueError, RuntimeError) as exc:
        raise _map_http(exc) from exc
    return PassageAskResponse(
        reply=result.reply,
        user_message=result.user_message,
        truncated=result.truncated,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        thinking=result.thinking,
        has_thinking=result.has_thinking,
    )


async def _stream_ask(
    *,
    minute_token: str,
    owner_user_id: int,
    body: PassageAskRequest,
) -> StreamingResponse:
    try:
        # 先做校验/拼装，失败时直接 HTTP 错误，避免 SSE 里才报「无选区」
        events = passage_ask_service.iter_ask_events(
            minute_token=minute_token,
            owner_user_id=owner_user_id,
            source_kind=body.source_kind,
            selected_text=body.selected_text,
            history=_to_history(body),
            question=body.question,
            images=_to_images(body),
        )
        # 触发 prepare：消费首个事件前先 peek；用 athrow 不便，改为包装
        agen = events.__aiter__()
        first = await agen.__anext__()
    except (LookupError, PermissionError, ValueError, RuntimeError) as exc:
        raise _map_http(exc) from exc
    except StopAsyncIteration as exc:
        raise HTTPException(status_code=502, detail="提问流为空") from exc

    async def event_source() -> AsyncIterator[str]:
        ev, data = first
        yield f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        async for ev, data in agen:
            yield f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/meetings/{minute_token}/ask",
    response_model=PassageAskResponse,
)
async def ask_on_meeting(
    minute_token: str,
    body: PassageAskRequest,
    request: Request,
    owner_user_id: int | None = None,
) -> PassageAskResponse:
    require_user_id(request)
    owner = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )
    return await _run_ask(
        minute_token=minute_token, owner_user_id=owner, body=body
    )


@router.post("/meetings/{minute_token}/ask/stream")
async def ask_on_meeting_stream(
    minute_token: str,
    body: PassageAskRequest,
    request: Request,
    owner_user_id: int | None = None,
) -> StreamingResponse:
    require_user_id(request)
    owner = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )
    return await _stream_ask(
        minute_token=minute_token, owner_user_id=owner, body=body
    )


@router.post(
    "/share/{share_token}/ask",
    response_model=PassageAskResponse,
)
async def ask_on_share(
    share_token: str,
    body: PassageAskRequest,
    request: Request,
    x_share_session: str | None = Header(default=None),
) -> PassageAskResponse:
    from app.api.routes.shares import _authorized_share, _require_share_owner

    share = await _authorized_share(share_token, request, x_share_session)
    owner = _require_share_owner(share)
    return await _run_ask(
        minute_token=share.minute_token, owner_user_id=owner, body=body
    )


@router.post("/share/{share_token}/ask/stream")
async def ask_on_share_stream(
    share_token: str,
    body: PassageAskRequest,
    request: Request,
    x_share_session: str | None = Header(default=None),
) -> StreamingResponse:
    from app.api.routes.shares import _authorized_share, _require_share_owner

    share = await _authorized_share(share_token, request, x_share_session)
    owner = _require_share_owner(share)
    return await _stream_ask(
        minute_token=share.minute_token, owner_user_id=owner, body=body
    )
