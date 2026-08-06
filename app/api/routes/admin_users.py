"""超级管理端：用户列表（只读）。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.core.auth_context import require_super_admin
from app.repository.orm.access_key import AccessKeyORM
from app.repository.orm.feishu_user_token import FeishuUserTokenORM
from app.repository.orm.invite_code import InviteCodeORM
from app.repository.orm.meeting_record import MeetingRecordORM
from app.repository.orm.share import ShareORM
from app.repository.uow import UnitOfWork

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


class AdminUserItemResponse(BaseModel):
    id: int
    username: str
    status: str
    created_at: int
    updated_at: int
    created_at_text: str
    updated_at_text: str
    meeting_count: int = 0
    share_count: int = 0
    access_key_count: int = 0
    invites_created: int = 0
    invites_redeemed: int = 0
    feishu_authorized: bool = False


class AdminUserListResponse(BaseModel):
    items: list[AdminUserItemResponse]
    total: int


def _ms_text(ms: int) -> str:
    if not ms:
        return "—"
    value = ms / 1000.0 if ms > 10_000_000_000 else float(ms)
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M"
    )


@router.get("", response_model=AdminUserListResponse)
async def list_users(request: Request) -> AdminUserListResponse:
    require_super_admin(request)
    async with UnitOfWork() as uow:
        assert uow.users is not None
        assert uow._session is not None
        users = await uow.users.list_all()
        session = uow._session

        meeting_rows = (
            await session.execute(
                select(MeetingRecordORM.owner_user_id, func.count())
                .where(MeetingRecordORM.owner_user_id.is_not(None))
                .group_by(MeetingRecordORM.owner_user_id)
            )
        ).all()
        share_rows = (
            await session.execute(
                select(ShareORM.owner_user_id, func.count())
                .where(
                    ShareORM.owner_user_id.is_not(None),
                    ShareORM.revoked_at.is_(None),
                )
                .group_by(ShareORM.owner_user_id)
            )
        ).all()
        key_rows = (
            await session.execute(
                select(AccessKeyORM.owner_user_id, func.count())
                .where(
                    AccessKeyORM.owner_user_id.is_not(None),
                    AccessKeyORM.revoked_at.is_(None),
                )
                .group_by(AccessKeyORM.owner_user_id)
            )
        ).all()
        invite_created_rows = (
            await session.execute(
                select(InviteCodeORM.created_by_user_id, func.count()).group_by(
                    InviteCodeORM.created_by_user_id
                )
            )
        ).all()
        invite_redeemed_rows = (
            await session.execute(
                select(InviteCodeORM.created_by_user_id, func.count())
                .where(InviteCodeORM.status == "USED")
                .group_by(InviteCodeORM.created_by_user_id)
            )
        ).all()
        feishu_rows = (
            await session.execute(select(FeishuUserTokenORM.user_id))
        ).scalars().all()

    meetings = {int(uid): int(n) for uid, n in meeting_rows if uid is not None}
    shares = {int(uid): int(n) for uid, n in share_rows if uid is not None}
    keys = {int(uid): int(n) for uid, n in key_rows if uid is not None}
    invites_c = {int(uid): int(n) for uid, n in invite_created_rows if uid is not None}
    invites_r = {
        int(uid): int(n) for uid, n in invite_redeemed_rows if uid is not None
    }
    feishu_ids = {int(uid) for uid in feishu_rows if uid is not None}

    items: list[AdminUserItemResponse] = []
    for user in users:
        if user.id is None:
            continue
        uid = user.id
        items.append(
            AdminUserItemResponse(
                id=uid,
                username=user.username,
                status=user.status,
                created_at=user.created_at,
                updated_at=user.updated_at,
                created_at_text=_ms_text(user.created_at),
                updated_at_text=_ms_text(user.updated_at),
                meeting_count=meetings.get(uid, 0),
                share_count=shares.get(uid, 0),
                access_key_count=keys.get(uid, 0),
                invites_created=invites_c.get(uid, 0),
                invites_redeemed=invites_r.get(uid, 0),
                feishu_authorized=uid in feishu_ids,
            )
        )
    return AdminUserListResponse(items=items, total=len(items))
