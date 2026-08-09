from fastapi import APIRouter, HTTPException, Query, Request

from app.core.auth_context import owner_scope_user_id, require_user_id
from app.dto.access_key import (
    AccessKeyCreateRequest,
    AccessKeyCreateResponse,
    AccessKeyListResponse,
    AccessKeyResponse,
    ShareAccessLogListResponse,
    ShareAccessLogResponse,
)
from app.service.access_key_service import access_key_service
from app.service.time_utils import utc_now_ms

router = APIRouter(prefix="/access-keys", tags=["access-keys"])


def _status_of(expires_at: int, revoked_at: int | None) -> str:
    if revoked_at is not None:
        return "REVOKED"
    if expires_at <= utc_now_ms():
        return "EXPIRED"
    return "ACTIVE"


def _to_response(entity, *, plaintext: str | None = None):
    base = dict(
        id=entity.id,
        name=entity.name,
        key_prefix=entity.key_prefix,
        expires_at=entity.expires_at,
        created_at=entity.created_at,
        revoked_at=entity.revoked_at,
        status=_status_of(entity.expires_at, entity.revoked_at),
    )
    if plaintext is not None:
        return AccessKeyCreateResponse(**base, plaintext_key=plaintext)
    return AccessKeyResponse(**base)


@router.get("", response_model=AccessKeyListResponse)
async def list_access_keys(
    request: Request,
    include_revoked: bool = Query(default=False),
) -> AccessKeyListResponse:
    items = await access_key_service.list_keys(
        include_revoked=include_revoked,
        owner_user_id=owner_scope_user_id(request),
    )
    return AccessKeyListResponse(items=[_to_response(i) for i in items])


@router.post("", response_model=AccessKeyCreateResponse)
async def create_access_key(
    body: AccessKeyCreateRequest, request: Request
) -> AccessKeyCreateResponse:
    owner_id = require_user_id(request)
    try:
        entity, plaintext = await access_key_service.create(
            name=body.name, expires_at=body.expires_at, owner_user_id=owner_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(entity, plaintext=plaintext)


@router.delete("/{key_id}", response_model=AccessKeyResponse)
async def revoke_access_key(key_id: int, request: Request) -> AccessKeyResponse:
    owner_id = require_user_id(request)
    try:
        entity = await access_key_service.revoke(key_id, owner_user_id=owner_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _to_response(entity)


@router.get("/{key_id}/logs", response_model=ShareAccessLogListResponse)
async def list_access_key_logs(
    key_id: int,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ShareAccessLogListResponse:
    try:
        logs = await access_key_service.list_logs(
            key_id,
            limit=limit,
            offset=offset,
            owner_user_id=owner_scope_user_id(request),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ShareAccessLogListResponse(
        items=[
            ShareAccessLogResponse(
                id=log.id,  # type: ignore[arg-type]
                access_key_id=log.access_key_id,
                share_id=log.share_id,
                minute_token=log.minute_token,
                meeting_title=log.meeting_title,
                action=log.action,
                ip=log.ip,
                user_agent=log.user_agent,
                created_at=log.created_at,
                session_id=log.session_id,
                referer=log.referer,
                device_type=log.device_type,
                browser=log.browser,
                os=log.os,
                started_at=log.started_at,
                ended_at=log.ended_at,
                dwell_ms=log.dwell_ms,
                video_progress_pct=log.video_progress_pct,
                result=log.result,
                fail_reason=log.fail_reason,
                detail_json=log.detail_json,
            )
            for log in logs
        ]
    )
