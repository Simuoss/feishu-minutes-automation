import re
import secrets

from pydantic import BaseModel

from fastapi import APIRouter, HTTPException, Request

from app.core.admin_auth import is_request_authorized, is_super_admin_token
from app.core.auth_context import require_auth, require_user_id
from app.core.config import settings
from app.core.jwt_auth import issue_super_admin_token, issue_user_token
from app.core.password import hash_password, verify_password
from app.data_model.entity.invite_code import InviteCodeCreateEntity, InviteCodeQueryEntity
from app.data_model.entity.user import UserCreateEntity, UserUpdateEntity
from app.repository.uow import UnitOfWork
from app.service.admin_ticket_store import TICKET_TTL_MS, admin_ticket_store
from app.service.time_utils import utc_now_ms

router = APIRouter(prefix="/auth", tags=["auth"])

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff-]{2,32}$")


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    invite_code: str


class SuperLoginRequest(BaseModel):
    token: str


class AuthTokenResponse(BaseModel):
    authorized: bool
    message: str = ""
    token: str = ""
    role: str = ""
    username: str | None = None
    user_id: int | None = None


class AuthStatusResponse(BaseModel):
    authorized: bool
    role: str | None = None
    username: str | None = None
    user_id: int | None = None


class AccessTicketResponse(BaseModel):
    access_ticket: str
    expires_in_seconds: int


class InviteCreateResponse(BaseModel):
    code: str
    invite_url: str
    created_at: int


class InviteItemResponse(BaseModel):
    code: str
    status: str
    created_at: int
    used_at: int | None = None
    used_by_user_id: int | None = None
    invite_url: str


class InviteListResponse(BaseModel):
    items: list[InviteItemResponse]


class InviteCheckResponse(BaseModel):
    valid: bool
    status: str | None = None


class MeResponse(BaseModel):
    user_id: int
    username: str
    display_name: str
    has_password: bool
    feishu_bound: bool
    feishu_name: str | None = None
    feishu_avatar_url: str | None = None
    needs_display_name_setup: bool = False


class MeUpdateRequest(BaseModel):
    display_name: str


class SetPasswordRequest(BaseModel):
    password: str
    old_password: str | None = None


def _invite_url(code: str) -> str:
    base = (settings.frontend_origin or "").rstrip("/")
    return f"{base}/register.html?invite={code}"


def _validate_username(username: str) -> str:
    name = (username or "").strip()
    if not _USERNAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail="用户名需为 2–32 位字母数字下划线或中文",
        )
    return name


def _validate_password(password: str) -> str:
    pwd = password or ""
    if len(pwd) < 6:
        raise HTTPException(status_code=422, detail="密码至少 6 位")
    return pwd


@router.post("/login", response_model=AuthTokenResponse)
@router.post("/admin/login", response_model=AuthTokenResponse, include_in_schema=False)
async def user_login(body: LoginRequest) -> AuthTokenResponse:
    if not (settings.jwt_secret or "").strip():
        raise HTTPException(status_code=503, detail="未配置 JWT_SECRET")
    username = (body.username or "").strip()
    password = body.password or ""
    if not username or not password:
        raise HTTPException(status_code=422, detail="请提供用户名与密码")

    async with UnitOfWork() as uow:
        assert uow.users is not None
        user = await uow.users.get_by_username(username)
        if user is None or user.status != "ACTIVE" or user.id is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        if not user.has_password():
            raise HTTPException(
                status_code=401,
                detail="该账号未设置密码，请使用飞书登录或先在账号设置中设置密码",
            )
        if not verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = issue_user_token(user_id=user.id, username=user.username)
        return AuthTokenResponse(
            authorized=True,
            message="登录成功",
            token=token,
            role="USER",
            username=user.public_display_name(),
            user_id=user.id,
        )


@router.post("/register", response_model=AuthTokenResponse)
async def register(body: RegisterRequest) -> AuthTokenResponse:
    if not (settings.jwt_secret or "").strip():
        raise HTTPException(status_code=503, detail="未配置 JWT_SECRET")
    username = _validate_username(body.username)
    password = _validate_password(body.password)
    invite_code = (body.invite_code or "").strip()
    if not invite_code:
        raise HTTPException(status_code=422, detail="请提供邀请码")

    now = utc_now_ms()
    async with UnitOfWork() as uow:
        assert uow.users is not None
        assert uow.invite_codes is not None
        if await uow.users.get_by_username(username) is not None:
            raise HTTPException(status_code=409, detail="用户名已被占用")
        invite = await uow.invite_codes.get_by_code(invite_code)
        if invite is None or invite.status != "ACTIVE":
            raise HTTPException(status_code=400, detail="邀请码无效或已使用")

        user = await uow.users.create(
            UserCreateEntity(
                username=username,
                password_hash=hash_password(password),
                status="ACTIVE",
                created_at=now,
                updated_at=now,
                display_name=username,
                display_name_set_at=now,
            )
        )
        if user.id is None:
            raise HTTPException(status_code=500, detail="创建用户失败")
        consumed = await uow.invite_codes.consume_active(
            code=invite_code, used_by_user_id=user.id, used_at=now
        )
        if not consumed:
            await uow.rollback()
            raise HTTPException(status_code=400, detail="邀请码无效或已使用")
        await uow.commit()
        token = issue_user_token(user_id=user.id, username=user.username)
        return AuthTokenResponse(
            authorized=True,
            message="注册成功",
            token=token,
            role="USER",
            username=user.username,
            user_id=user.id,
        )


@router.post("/super/login", response_model=AuthTokenResponse)
async def super_login(body: SuperLoginRequest) -> AuthTokenResponse:
    if not (settings.jwt_secret or "").strip():
        raise HTTPException(status_code=503, detail="未配置 JWT_SECRET")
    if not settings.resolved_super_admin_token:
        raise HTTPException(status_code=503, detail="未配置超级管理员口令")
    if not is_super_admin_token((body.token or "").strip()):
        raise HTTPException(status_code=401, detail="超级管理员口令错误")
    token = issue_super_admin_token()
    return AuthTokenResponse(
        authorized=True,
        message="超级管理员已解锁",
        token=token,
        role="SUPER_ADMIN",
        username=None,
        user_id=None,
    )


@router.get("/me", response_model=MeResponse)
async def get_me(request: Request) -> MeResponse:
    user_id = require_user_id(request)
    async with UnitOfWork() as uow:
        assert uow.users is not None
        user = await uow.users.get_by_id(user_id)
    if user is None or user.status != "ACTIVE":
        raise HTTPException(status_code=404, detail="用户不存在")
    return MeResponse(
        user_id=user_id,
        username=user.username,
        display_name=user.public_display_name(),
        has_password=user.has_password(),
        feishu_bound=bool(user.feishu_open_id),
        feishu_name=user.feishu_name,
        feishu_avatar_url=user.feishu_avatar_url,
        needs_display_name_setup=user.display_name_set_at is None,
    )


@router.patch("/me", response_model=MeResponse)
async def patch_me(body: MeUpdateRequest, request: Request) -> MeResponse:
    user_id = require_user_id(request)
    name = (body.display_name or "").strip()
    if not name or len(name) > 64:
        raise HTTPException(status_code=422, detail="显示名需为 1–64 个字符")
    now = utc_now_ms()
    async with UnitOfWork() as uow:
        assert uow.users is not None
        updated = await uow.users.update(
            UserUpdateEntity(
                id=user_id,
                display_name=name,
                display_name_set_at=now,
                updated_at=now,
            )
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        await uow.commit()
    return MeResponse(
        user_id=user_id,
        username=updated.username,
        display_name=updated.public_display_name(),
        has_password=updated.has_password(),
        feishu_bound=bool(updated.feishu_open_id),
        feishu_name=updated.feishu_name,
        feishu_avatar_url=updated.feishu_avatar_url,
        needs_display_name_setup=False,
    )


@router.post("/me/password", response_model=MeResponse)
async def set_password(body: SetPasswordRequest, request: Request) -> MeResponse:
    user_id = require_user_id(request)
    password = _validate_password(body.password)
    now = utc_now_ms()
    async with UnitOfWork() as uow:
        assert uow.users is not None
        user = await uow.users.get_by_id(user_id)
        if user is None or user.status != "ACTIVE":
            raise HTTPException(status_code=404, detail="用户不存在")
        if user.has_password():
            old = body.old_password or ""
            if not verify_password(old, user.password_hash):
                raise HTTPException(status_code=401, detail="原密码不正确")
        updated = await uow.users.update(
            UserUpdateEntity(
                id=user_id,
                password_hash=hash_password(password),
                updated_at=now,
            )
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        await uow.commit()
    return MeResponse(
        user_id=user_id,
        username=updated.username,
        display_name=updated.public_display_name(),
        has_password=True,
        feishu_bound=bool(updated.feishu_open_id),
        feishu_name=updated.feishu_name,
        feishu_avatar_url=updated.feishu_avatar_url,
        needs_display_name_setup=updated.display_name_set_at is None,
    )


@router.get("/admin/status", response_model=AuthStatusResponse)
@router.get("/status", response_model=AuthStatusResponse, include_in_schema=False)
async def auth_status(request: Request) -> AuthStatusResponse:
    auth = getattr(request.state, "auth", None)
    if auth is None and is_request_authorized(request):
        from app.core.admin_auth import resolve_request_principal

        auth = resolve_request_principal(request)
    if auth is None:
        return AuthStatusResponse(authorized=False)
    return AuthStatusResponse(
        authorized=True,
        role=auth.role,
        username=auth.username,
        user_id=auth.user_id,
    )


@router.post("/admin/access-ticket", response_model=AccessTicketResponse)
@router.post("/access-ticket", response_model=AccessTicketResponse, include_in_schema=False)
async def issue_access_ticket(request: Request) -> AccessTicketResponse:
    auth = require_auth(request)
    ticket = admin_ticket_store.issue(auth)
    return AccessTicketResponse(
        access_ticket=ticket.ticket,
        expires_in_seconds=max(1, TICKET_TTL_MS // 1000),
    )


@router.post("/invites", response_model=InviteCreateResponse)
async def create_invite(request: Request) -> InviteCreateResponse:
    user_id = require_user_id(request)
    code = secrets.token_urlsafe(12)
    now = utc_now_ms()
    async with UnitOfWork() as uow:
        assert uow.invite_codes is not None
        created = await uow.invite_codes.create(
            InviteCodeCreateEntity(
                code=code,
                created_by_user_id=user_id,
                created_at=now,
                status="ACTIVE",
            )
        )
        await uow.commit()
    return InviteCreateResponse(
        code=created.code,
        invite_url=_invite_url(created.code),
        created_at=created.created_at,
    )


@router.get("/invites", response_model=InviteListResponse)
async def list_invites(request: Request) -> InviteListResponse:
    user_id = require_user_id(request)
    async with UnitOfWork() as uow:
        assert uow.invite_codes is not None
        rows = await uow.invite_codes.query(
            InviteCodeQueryEntity(created_by_user_id=user_id)
        )
    return InviteListResponse(
        items=[
            InviteItemResponse(
                code=row.code,
                status=row.status,
                created_at=row.created_at,
                used_at=row.used_at,
                used_by_user_id=row.used_by_user_id,
                invite_url=_invite_url(row.code),
            )
            for row in rows
        ]
    )


@router.get("/invites/check/{code}", response_model=InviteCheckResponse)
async def check_invite(code: str) -> InviteCheckResponse:
    cleaned = (code or "").strip()
    if not cleaned:
        return InviteCheckResponse(valid=False, status=None)
    async with UnitOfWork() as uow:
        assert uow.invite_codes is not None
        row = await uow.invite_codes.get_by_code(cleaned)
    if row is None:
        return InviteCheckResponse(valid=False, status=None)
    return InviteCheckResponse(valid=row.status == "ACTIVE", status=row.status)


@router.delete("/invites/{code}", response_model=InviteItemResponse)
async def revoke_invite(code: str, request: Request) -> InviteItemResponse:
    user_id = require_user_id(request)
    cleaned = (code or "").strip()
    async with UnitOfWork() as uow:
        assert uow.invite_codes is not None
        ok = await uow.invite_codes.revoke(code=cleaned, created_by_user_id=user_id)
        if not ok:
            raise HTTPException(status_code=404, detail="邀请码不存在或已失效")
        row = await uow.invite_codes.get_by_code(cleaned)
        await uow.commit()
    assert row is not None
    return InviteItemResponse(
        code=row.code,
        status=row.status,
        created_at=row.created_at,
        used_at=row.used_at,
        used_by_user_id=row.used_by_user_id,
        invite_url=_invite_url(row.code),
    )
