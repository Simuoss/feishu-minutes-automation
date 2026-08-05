from pydantic import BaseModel, Field

from fastapi import APIRouter, HTTPException, Request

from app.core.admin_auth import extract_bearer_token, is_admin_token, is_request_authorized
from app.core.config import settings
from app.service.admin_ticket_store import TICKET_TTL_MS, admin_ticket_store

router = APIRouter(prefix="/auth/admin", tags=["auth"])


class AdminLoginRequest(BaseModel):
    token: str = Field(min_length=1)


class AdminLoginResponse(BaseModel):
    authorized: bool
    message: str = ""


class AdminStatusResponse(BaseModel):
    authorized: bool


class AccessTicketResponse(BaseModel):
    access_ticket: str
    expires_in_seconds: int


@router.post("/login", response_model=AdminLoginResponse)
async def admin_login(body: AdminLoginRequest) -> AdminLoginResponse:
    if not (settings.admin_token or "").strip():
        raise HTTPException(status_code=503, detail="未配置 ADMIN_TOKEN")
    if not is_admin_token(body.token.strip()):
        raise HTTPException(status_code=401, detail="管理员口令错误")
    return AdminLoginResponse(authorized=True, message="登录成功")


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
