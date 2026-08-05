import secrets

from pydantic import BaseModel

from fastapi import APIRouter, HTTPException, Request

from app.core.admin_auth import extract_bearer_token, is_admin_token, is_request_authorized
from app.core.config import settings
from app.core.password import verify_password
from app.repository.uow import UnitOfWork
from app.service.admin_ticket_store import TICKET_TTL_MS, admin_ticket_store

router = APIRouter(prefix="/auth/admin", tags=["auth"])


class AdminLoginRequest(BaseModel):
    token: str | None = None
    username: str | None = None
    password: str | None = None


class AdminLoginResponse(BaseModel):
    authorized: bool
    message: str = ""
    # 前端应存此值作为 Bearer；现阶段仍为 ADMIN_TOKEN
    token: str = ""


class AdminStatusResponse(BaseModel):
    authorized: bool


class AccessTicketResponse(BaseModel):
    access_ticket: str
    expires_in_seconds: int


async def _authenticate_admin(body: AdminLoginRequest) -> str | None:
    """成功时返回应下发的 Bearer（ADMIN_TOKEN）。"""
    expected = (settings.admin_token or "").strip()
    if not expected:
        return None

    token = (body.token or "").strip()
    if token and is_admin_token(token):
        return expected

    password = (body.password or "").strip()
    username = (body.username or "").strip() or "admin"

    # ADMIN_TOKEN 可直接当密码（映射 admin）
    if password and secrets.compare_digest(password, expected):
        return expected

    if not password:
        return None

    async with UnitOfWork() as uow:
        assert uow.users is not None
        user = await uow.users.get_by_username(username)
        if user is None or user.status != "ACTIVE":
            return None
        if not verify_password(password, user.password_hash):
            return None
    return expected


@router.post("/login", response_model=AdminLoginResponse)
async def admin_login(body: AdminLoginRequest) -> AdminLoginResponse:
    if not (settings.admin_token or "").strip():
        raise HTTPException(status_code=503, detail="未配置 ADMIN_TOKEN")
    if not (body.token or body.password):
        raise HTTPException(status_code=422, detail="请提供 token 或 username/password")
    bearer = await _authenticate_admin(body)
    if not bearer:
        raise HTTPException(status_code=401, detail="管理员口令错误")
    return AdminLoginResponse(authorized=True, message="登录成功", token=bearer)


@router.get("/status", response_model=AdminStatusResponse)
async def admin_status(request: Request) -> AdminStatusResponse:
    return AdminStatusResponse(authorized=is_request_authorized(request))


@router.post("/access-ticket", response_model=AccessTicketResponse)
async def issue_access_ticket(request: Request) -> AccessTicketResponse:
    """用 Bearer 口令兑换短期票，供媒体/SSE 查询参数使用。"""
    if not is_admin_token(extract_bearer_token(request)):
        raise HTTPException(status_code=401, detail="需要管理员登录")
    ticket = admin_ticket_store.issue()
    return AccessTicketResponse(
        access_ticket=ticket.ticket,
        expires_in_seconds=max(1, TICKET_TTL_MS // 1000),
    )
