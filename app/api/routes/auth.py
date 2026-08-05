import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core.request_urls import (
    decode_oauth_state,
    encode_oauth_state,
    frontend_origin_for_redirect_uri,
    oauth_redirect_allowlist,
    resolve_oauth_redirect_uri,
)
from app.integrations.feishu.user_auth import (
    FeishuUserAuthClient,
)
from app.service.minute_subscription_service import ensure_minute_generated_subscription

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/feishu", tags=["auth"])
_user_auth = FeishuUserAuthClient()


class AuthStatusResponse(BaseModel):
    authorized: bool
    login_url: str
    reauth_url: str
    granted_scopes: list[str] = []
    missing_scopes: list[str] = []
    needs_reauth: bool = False


@router.get("/status", response_model=AuthStatusResponse)
async def auth_status() -> AuthStatusResponse:
    # 相对路径：浏览器按当前页面 origin 发起授权，从而动态选本地或公网回调
    authorized = _user_auth.is_authorized()
    granted = sorted(_user_auth.get_granted_scopes()) if authorized else []
    missing = _user_auth.get_missing_scopes() if authorized else []
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
    if reauth:
        _user_auth.clear_authorization()
    redirect_uri = resolve_oauth_redirect_uri(request)
    frontend_origin = frontend_origin_for_redirect_uri(redirect_uri)
    state = encode_oauth_state(
        redirect_uri=redirect_uri,
        frontend_origin=frontend_origin,
        reauth=reauth,
    )
    url = _user_auth.build_authorize_url(
        state=state,
        redirect_uri=redirect_uri,
        force_consent=reauth,
    )
    logger.info(
        "发起飞书 OAuth：redirect_uri=%s frontend=%s reauth=%s",
        redirect_uri,
        frontend_origin,
        reauth,
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

    try:
        await _user_auth.exchange_code(code, redirect_uri=redirect_uri)
    except RuntimeError as exc:
        logger.error(
            "飞书 OAuth 回调失败 redirect_uri=%s: %s",
            redirect_uri,
            exc,
        )
        msg = quote(str(exc), safe="")
        return RedirectResponse(f"{frontend_origin.rstrip('/')}/?auth=error&msg={msg}")

    await ensure_minute_generated_subscription(user_auth=_user_auth)
    return RedirectResponse(f"{frontend_origin.rstrip('/')}/?auth=ok")
