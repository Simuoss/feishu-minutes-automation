import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core.admin_auth import resolve_request_principal
from app.core.auth_context import require_user_id
from app.core.request_urls import (
    decode_oauth_state,
    encode_oauth_state,
    frontend_origin_for_redirect_uri,
    oauth_redirect_allowlist,
    resolve_oauth_redirect_uri,
)
from app.integrations.feishu.user_auth import FeishuUserAuthClient
from app.service.minute_subscription_service import ensure_minute_generated_subscription

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/feishu", tags=["auth"])


class AuthStatusResponse(BaseModel):
    authorized: bool
    login_url: str
    reauth_url: str
    granted_scopes: list[str] = []
    missing_scopes: list[str] = []
    needs_reauth: bool = False


@router.get("/status", response_model=AuthStatusResponse)
async def auth_status(request: Request) -> AuthStatusResponse:
    user_id = require_user_id(request)
    user_auth = FeishuUserAuthClient(user_id=user_id)
    authorized, granted, missing = await user_auth.auth_status_async()
    return AuthStatusResponse(
        authorized=authorized,
        login_url="/api/v1/auth/feishu/login",
        reauth_url="/api/v1/auth/feishu/login?reauth=1",
        granted_scopes=granted,
        missing_scopes=missing,
        needs_reauth=bool(missing),
    )


@router.get("/login")
async def login(
    request: Request, reauth: bool = Query(default=False)
) -> RedirectResponse:
    principal = resolve_request_principal(request)
    if principal is None or not principal.is_user or principal.user_id is None:
        raise HTTPException(
            status_code=401,
            detail="请先登录本系统账号，再绑定你自己的飞书授权（禁止把授权绑到其他账号）",
        )
    oauth_user_id = principal.user_id
    user_auth = FeishuUserAuthClient(user_id=oauth_user_id)

    redirect_uri = resolve_oauth_redirect_uri(request)
    frontend_origin = frontend_origin_for_redirect_uri(redirect_uri)

    if reauth:
        user_auth.clear_authorization()
    state = encode_oauth_state(
        redirect_uri=redirect_uri,
        frontend_origin=frontend_origin,
        reauth=reauth,
        user_id=oauth_user_id,
    )
    url = user_auth.build_authorize_url(
        state=state,
        redirect_uri=redirect_uri,
        force_consent=reauth,
    )
    logger.info(
        "发起飞书 OAuth：redirect_uri=%s frontend=%s reauth=%s user_id=%s",
        redirect_uri,
        frontend_origin,
        reauth,
        oauth_user_id,
    )
    return RedirectResponse(url)


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
) -> RedirectResponse:
    if not code:
        raise HTTPException(status_code=400, detail="缺少 OAuth code")

    parsed = decode_oauth_state(state)
    redirect_uri = str(parsed.get("r") or "").strip() or resolve_oauth_redirect_uri(request)
    if redirect_uri not in oauth_redirect_allowlist():
        # 防 Host 伪造：只接受白名单内的回调
        redirect_uri = resolve_oauth_redirect_uri(request)

    frontend_origin = str(parsed.get("f") or "").strip()
    if not frontend_origin:
        frontend_origin = frontend_origin_for_redirect_uri(redirect_uri)

    if state and not parsed:
        msg = quote("授权状态无效或已过期，请重新发起飞书授权", safe="")
        return RedirectResponse(f"{frontend_origin.rstrip('/')}/?auth=error&msg={msg}")

    uid_raw = parsed.get("uid")
    try:
        oauth_user_id = int(uid_raw) if uid_raw is not None else None
    except (TypeError, ValueError):
        oauth_user_id = None
    if oauth_user_id is None:
        msg = quote("飞书授权回调缺少用户身份，拒绝写入 Token", safe="")
        return RedirectResponse(f"{frontend_origin.rstrip('/')}/?auth=error&msg={msg}")

    user_auth = FeishuUserAuthClient(user_id=oauth_user_id)
    try:
        await user_auth.exchange_code(code, redirect_uri=redirect_uri)
    except RuntimeError as exc:
        logger.error(
            "飞书 OAuth 回调失败 redirect_uri=%s user_id=%s: %s",
            redirect_uri,
            oauth_user_id,
            exc,
        )
        msg = quote(str(exc), safe="")
        return RedirectResponse(f"{frontend_origin.rstrip('/')}/?auth=error&msg={msg}")

    await ensure_minute_generated_subscription(user_id=oauth_user_id)
    return RedirectResponse(f"{frontend_origin.rstrip('/')}/?auth=ok")
