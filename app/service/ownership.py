"""按 owner_user_id 校验会议归属，并解析资源访问用的 owner。"""

from __future__ import annotations

from fastapi import HTTPException, Query, Request

from app.core.auth_context import owner_scope_user_id, require_auth
from app.repository.uow import UnitOfWork

# 仅成功落库的会议可读内容；FAILED/DOWNLOADING 不得借共享磁盘读他人文件
_READABLE_STATUSES = frozenset({"COMPLETED"})


async def resolve_resource_owner(
    request: Request,
    minute_token: str,
    *,
    owner_user_id: int | None = None,
) -> int:
    """解析读写本地/R2/通道时使用的 owner。

    - 普通用户：必须是自己；忽略传入的他人 id
    - 超管：必须显式传 owner_user_id（与 stored 列表的 owner_id 对齐）
    """
    auth = require_auth(request)
    token = (minute_token or "").strip()
    if not token:
        raise HTTPException(status_code=404, detail="会议不存在")

    if auth.is_user:
        if auth.user_id is None:
            raise HTTPException(status_code=401, detail="需要登录")
        return int(auth.user_id)

    if auth.is_super_admin:
        if owner_user_id is None:
            raise HTTPException(
                status_code=400,
                detail="超级管理员访问会议资源时必须指定 owner_user_id",
            )
        return int(owner_user_id)

    raise HTTPException(status_code=401, detail="需要登录")


async def assert_meeting_readable(
    request: Request,
    minute_token: str,
    *,
    owner_user_id: int | None = None,
) -> int:
    """普通用户仅可读自己已完成的会议；超管可读指定 owner 的完成会议。返回资源 owner。"""
    owner_id = await resolve_resource_owner(
        request, minute_token, owner_user_id=owner_user_id
    )
    auth = require_auth(request)
    token = (minute_token or "").strip()
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        row = await uow.meeting_records.get_latest_by_minute_token(
            token, owner_user_id=owner_id
        )
    if (
        row is None
        or row.owner_user_id is None
        or row.status not in _READABLE_STATUSES
    ):
        raise HTTPException(status_code=404, detail="会议不存在")
    if auth.is_user and row.owner_user_id != auth.user_id:
        raise HTTPException(status_code=404, detail="会议不存在")
    return owner_id


async def assert_meeting_progress_visible(
    request: Request,
    minute_token: str,
    *,
    owner_user_id: int | None = None,
) -> int:
    """进度查询：允许查看自己名下任意状态；超管须指定 owner。返回资源 owner。"""
    owner_id = await resolve_resource_owner(
        request, minute_token, owner_user_id=owner_user_id
    )
    auth = require_auth(request)
    token = (minute_token or "").strip()
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        row = await uow.meeting_records.get_latest_by_minute_token(
            token, owner_user_id=owner_id
        )
    if row is None or row.owner_user_id is None:
        raise HTTPException(status_code=404, detail="会议不存在")
    if auth.is_user and row.owner_user_id != auth.user_id:
        raise HTTPException(status_code=404, detail="会议不存在")
    return owner_id


async def meeting_owned_by(minute_token: str, owner_user_id: int | None) -> bool:
    """判断会议是否属于指定 owner；None 视为不属于。"""
    if owner_user_id is None:
        return False
    token = (minute_token or "").strip()
    if not token:
        return False
    owner_id = int(owner_user_id)
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        row = await uow.meeting_records.get_latest_by_minute_token(
            token, owner_user_id=owner_id
        )
    return (
        row is not None
        and row.owner_user_id == owner_id
        and row.status in _READABLE_STATUSES
    )


def scope_from_request(request: Request) -> int | None:
    return owner_scope_user_id(request)


def owner_query_param(
    owner_user_id: int | None = Query(
        default=None,
        description="超级管理员访问他人资源时必填；普通用户忽略",
    ),
) -> int | None:
    return owner_user_id
