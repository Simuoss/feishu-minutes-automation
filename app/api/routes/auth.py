import logging
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core.admin_auth import resolve_request_principal
from app.core.auth_context import require_user_id
from app.core.config import settings
from app.core.jwt_auth import issue_user_token
from app.core.request_urls import (
    decode_oauth_state,
    encode_oauth_state,
    frontend_origin_for_redirect_uri,
    oauth_redirect_allowlist,
    resolve_oauth_redirect_uri,
)
from app.integrations.feishu.user_auth import FeishuUserAuthClient
from app.repository.uow import UnitOfWork
from app.service.feishu_sso_service import (
    bind_feishu_profile_to_user,
    create_user_from_feishu_profile,
    profile_from_user_info,
)
from app.service.minute_subscription_service import ensure_minute_generated_subscription
from app.service.sso_ticket_store import sso_ticket_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/feishu", tags=["auth"])


class AuthStatusResponse(BaseModel):
    authorized: bool
    login_url: str
    reauth_url: str
    granted_scopes: list[str] = []
    missing_scopes: list[str] = []
    needs_reauth: bool = False
    feishu_bound: bool = False


class SsoExchangeRequest(BaseModel):
    ticket: str


class SsoExchangeResponse(BaseModel):
    token: str
    user_id: int
    username: str
    setup_name: bool = False


def _build_authorize_url_anonymous(
    *,
    state: str,
    redirect_uri: str,
    force_consent: bool,
) -> str:
    # 复用客户端拼 URL 逻辑：临时 user_id 仅用于构造，不落库
    return FeishuUserAuthClient(user_id=0).build_authorize_url(
        state=state,
        redirect_uri=redirect_uri,
        force_consent=force_consent,
    )


@router.get("/status", response_model=AuthStatusResponse)
async def auth_status(request: Request) -> AuthStatusResponse:
    user_id = require_user_id(request)
    user_auth = FeishuUserAuthClient(user_id=user_id)
    authorized, granted, missing = await user_auth.auth_status_async()
    feishu_bound = False
    async with UnitOfWork() as uow:
        assert uow.users is not None
        user = await uow.users.get_by_id(user_id)
        feishu_bound = bool(user and user.feishu_open_id)
    return AuthStatusResponse(
        authorized=authorized,
        login_url="/api/v1/auth/feishu/login?intent=login",
        reauth_url="/api/v1/auth/feishu/login?intent=bind&reauth=1",
        granted_scopes=granted,
        missing_scopes=missing,
        needs_reauth=bool(missing),
        feishu_bound=feishu_bound,
    )


@router.get("/login")
async def login(
    request: Request,
    reauth: bool = Query(default=False),
    intent: str = Query(default="login"),
    invite: str | None = Query(default=None),
) -> RedirectResponse:
    intent_norm = (intent or "login").strip().upper()
    if intent_norm not in {"LOGIN", "BIND"}:
        # 兼容旧链接：已登录 + reauth → BIND；否则 LOGIN
        principal = resolve_request_principal(request)
        if principal is not None and principal.is_user and principal.user_id is not None:
            intent_norm = "BIND"
        else:
            intent_norm = "LOGIN"

    redirect_uri = resolve_oauth_redirect_uri(request)
    frontend_origin = frontend_origin_for_redirect_uri(redirect_uri)
    invite_code = (invite or "").strip() or None

    if intent_norm == "LOGIN":
        if settings.feishu_sso_require_invite:
            if not invite_code:
                raise HTTPException(
                    status_code=400,
                    detail="当前环境要求邀请码才能使用飞书登录/注册",
                )
            async with UnitOfWork() as uow:
                assert uow.invite_codes is not None
                row = await uow.invite_codes.get_by_code(invite_code)
                if row is None or row.status != "ACTIVE":
                    raise HTTPException(status_code=400, detail="邀请码无效或已使用")
        state = encode_oauth_state(
            redirect_uri=redirect_uri,
            frontend_origin=frontend_origin,
            reauth=reauth,
            intent="LOGIN",
            invite=invite_code,
        )
        url = _build_authorize_url_anonymous(
            state=state,
            redirect_uri=redirect_uri,
            force_consent=reauth,
        )
        logger.info(
            "发起飞书 SSO 登录 OAuth：redirect_uri=%s frontend=%s require_invite=%s",
            redirect_uri,
            frontend_origin,
            settings.feishu_sso_require_invite,
        )
        return RedirectResponse(url)

    # BIND：必须已登录本系统 USER
    principal = resolve_request_principal(request)
    if principal is None or not principal.is_user or principal.user_id is None:
        raise HTTPException(
            status_code=401,
            detail="请先登录本系统账号，再绑定你自己的飞书授权（禁止把授权绑到其他账号）",
        )
    oauth_user_id = principal.user_id
    user_auth = FeishuUserAuthClient(user_id=oauth_user_id)
    if reauth:
        user_auth.clear_authorization()
    state = encode_oauth_state(
        redirect_uri=redirect_uri,
        frontend_origin=frontend_origin,
        reauth=reauth,
        user_id=oauth_user_id,
        intent="BIND",
    )
    url = user_auth.build_authorize_url(
        state=state,
        redirect_uri=redirect_uri,
        force_consent=True,
    )
    logger.info(
        "发起飞书绑定 OAuth：redirect_uri=%s frontend=%s user_id=%s",
        redirect_uri,
        frontend_origin,
        oauth_user_id,
    )
    return RedirectResponse(url)


@router.post("/sso-exchange", response_model=SsoExchangeResponse)
async def sso_exchange(body: SsoExchangeRequest) -> SsoExchangeResponse:
    if not (settings.jwt_secret or "").strip():
        raise HTTPException(status_code=503, detail="未配置 JWT_SECRET")
    payload = sso_ticket_store.consume(body.ticket)
    if payload is None:
        raise HTTPException(
            status_code=400,
            detail="登录凭证无效或已过期，请重新使用飞书登录",
        )
    token = issue_user_token(user_id=payload.user_id, username=payload.username)
    return SsoExchangeResponse(
        token=token,
        user_id=payload.user_id,
        username=payload.username,
        setup_name=payload.setup_name,
    )


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
) -> RedirectResponse:
    if not code:
        raise HTTPException(status_code=400, detail="缺少 OAuth code")

    parsed = decode_oauth_state(state)
    redirect_uri = str(parsed.get("r") or "").strip() or resolve_oauth_redirect_uri(
        request
    )
    if redirect_uri not in oauth_redirect_allowlist():
        redirect_uri = resolve_oauth_redirect_uri(request)

    frontend_origin = str(parsed.get("f") or "").strip()
    if not frontend_origin:
        frontend_origin = frontend_origin_for_redirect_uri(redirect_uri)
    front = frontend_origin.rstrip("/")

    if state and not parsed:
        msg = quote("授权状态无效或已过期，请重新发起飞书授权", safe="")
        return RedirectResponse(f"{front}/login.html?auth=error&msg={msg}")

    intent = str(parsed.get("intent") or "BIND").strip().upper()
    if intent not in {"LOGIN", "BIND"}:
        # 旧 state 无 intent：有 uid 视为 BIND
        intent = "BIND" if parsed.get("uid") is not None else "LOGIN"

    try:
        token_data = await FeishuUserAuthClient.exchange_code_stateless(
            code, redirect_uri=redirect_uri
        )
        access_token = str(token_data.get("access_token") or "")
        user_info = await FeishuUserAuthClient.fetch_user_info(access_token)
        profile = profile_from_user_info(user_info)
        if not profile.get("feishu_open_id"):
            raise RuntimeError("飞书未返回 open_id，无法完成登录")
    except Exception as exc:  # noqa: BLE001
        logger.error("飞书 OAuth 换票或拉资料失败：%s", exc)
        msg = quote(str(exc), safe="")
        return RedirectResponse(f"{front}/login.html?auth=error&msg={msg}")

    try:
        if intent == "LOGIN":
            invite_code = str(parsed.get("invite") or "").strip()
            if settings.feishu_sso_require_invite:
                if not invite_code:
                    raise RuntimeError("缺少邀请码，无法完成飞书注册")
            async with UnitOfWork() as uow:
                assert uow.users is not None
                assert uow.invite_codes is not None
                existing = await uow.users.get_by_feishu_open_id(
                    profile["feishu_open_id"]
                )
            if existing is None:
                if settings.feishu_sso_require_invite:
                    from app.service.time_utils import utc_now_ms

                    now = utc_now_ms()
                    async with UnitOfWork() as uow:
                        assert uow.invite_codes is not None
                        inv = await uow.invite_codes.get_by_code(invite_code)
                        if inv is None or inv.status != "ACTIVE":
                            raise RuntimeError("邀请码无效或已使用")
                    user = await create_user_from_feishu_profile(profile)
                    async with UnitOfWork() as uow:
                        assert uow.invite_codes is not None
                        ok = await uow.invite_codes.consume_active(
                            code=invite_code,
                            used_by_user_id=user.id,  # type: ignore[arg-type]
                            used_at=now,
                        )
                        if not ok:
                            raise RuntimeError("邀请码无效或已使用")
                        await uow.commit()
                else:
                    user = await create_user_from_feishu_profile(profile)
            else:
                user = existing
            if user.id is None:
                raise RuntimeError("用户身份无效")
        else:
            uid_raw = parsed.get("uid")
            try:
                oauth_user_id = int(uid_raw) if uid_raw is not None else None
            except (TypeError, ValueError):
                oauth_user_id = None
            if oauth_user_id is None:
                raise RuntimeError("飞书绑定回调缺少用户身份，拒绝写入")
            user = await bind_feishu_profile_to_user(oauth_user_id, profile)
            if user.id is None:
                raise RuntimeError("用户身份无效")

        FeishuUserAuthClient(user_id=user.id).persist_token_response(token_data)
        await ensure_minute_generated_subscription(user_id=user.id)
    except ValueError as exc:
        msg = quote(str(exc), safe="")
        dest = "/login.html" if intent == "LOGIN" else "/"
        return RedirectResponse(f"{front}{dest}?auth=error&msg={msg}")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "飞书 SSO 落库失败 intent=%s：%s",
            intent,
            exc,
        )
        msg = quote(str(exc), safe="")
        dest = "/login.html" if intent == "LOGIN" else "/"
        return RedirectResponse(f"{front}{dest}?auth=error&msg={msg}")

    setup_name = user.display_name_set_at is None
    if intent == "LOGIN":
        ticket = sso_ticket_store.issue(
            user_id=user.id,
            username=user.username,
            setup_name=setup_name,
        )
        qs = urlencode(
            {
                "auth": "ok",
                "feishu": "1",
                "sso_ticket": ticket,
                **({"setup_name": "1"} if setup_name else {}),
            }
        )
        return RedirectResponse(f"{front}/?{qs}")

    qs = urlencode(
        {
            "auth": "ok",
            "feishu": "1",
            **({"setup_name": "1"} if setup_name else {}),
        }
    )
    return RedirectResponse(f"{front}/?{qs}")
