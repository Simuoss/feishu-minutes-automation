"""飞书 SSO：建号 / 绑定 / 资料落库。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.data_model.entity.user import UserCreateEntity, UserEntity, UserUpdateEntity
from app.repository.uow import UnitOfWork
from app.service.time_utils import utc_now_ms

logger = logging.getLogger(__name__)

_OPEN_ID_TAIL_RE = re.compile(r"[^A-Za-z0-9_-]+")


def username_from_open_id(open_id: str) -> str:
    tail = (open_id or "").rsplit("-", 1)[-1] or open_id
    tail = _OPEN_ID_TAIL_RE.sub("", tail)[:24] or "user"
    return f"fs_{tail}"


def profile_from_user_info(info: dict[str, Any]) -> dict[str, Any]:
    """从飞书 user_info 抽取可落库字段（缺省则跳过）。"""
    data = info.get("data") if isinstance(info.get("data"), dict) else info
    if not isinstance(data, dict):
        data = {}
    open_id = str(data.get("open_id") or "").strip()
    name = str(data.get("name") or "").strip() or None
    return {
        "feishu_open_id": open_id or None,
        "feishu_union_id": (str(data.get("union_id") or "").strip() or None),
        "feishu_user_id": (str(data.get("user_id") or "").strip() or None),
        "feishu_name": name,
        "feishu_en_name": (str(data.get("en_name") or "").strip() or None),
        "feishu_avatar_url": (
            str(
                data.get("avatar_url")
                or data.get("avatar_middle")
                or data.get("avatar_thumb")
                or ""
            ).strip()
            or None
        ),
        "feishu_tenant_key": (str(data.get("tenant_key") or "").strip() or None),
        "feishu_email": (
            str(data.get("email") or data.get("enterprise_email") or "").strip() or None
        ),
        "feishu_mobile": (str(data.get("mobile") or "").strip() or None),
        "feishu_profile_json": json.dumps(data, ensure_ascii=False),
        "display_name_default": name,
    }


async def create_user_from_feishu_profile(profile: dict[str, Any]) -> UserEntity:
    open_id = profile.get("feishu_open_id")
    if not open_id:
        raise ValueError("飞书资料缺少 open_id，无法建号")
    now = utc_now_ms()
    base_username = username_from_open_id(open_id)
    display = (profile.get("display_name_default") or base_username)[:128]
    async with UnitOfWork() as uow:
        assert uow.users is not None
        existing = await uow.users.get_by_feishu_open_id(open_id)
        if existing is not None:
            return existing
        username = base_username
        suffix = 0
        while await uow.users.get_by_username(username) is not None:
            suffix += 1
            username = f"{base_username}_{suffix}"[:64]
        user = await uow.users.create(
            UserCreateEntity(
                username=username,
                password_hash="",
                status="ACTIVE",
                created_at=now,
                updated_at=now,
                display_name=display,
                display_name_set_at=None,
                feishu_open_id=open_id,
                feishu_union_id=profile.get("feishu_union_id"),
                feishu_user_id=profile.get("feishu_user_id"),
                feishu_name=profile.get("feishu_name"),
                feishu_en_name=profile.get("feishu_en_name"),
                feishu_avatar_url=profile.get("feishu_avatar_url"),
                feishu_tenant_key=profile.get("feishu_tenant_key"),
                feishu_email=profile.get("feishu_email"),
                feishu_mobile=profile.get("feishu_mobile"),
                feishu_profile_json=profile.get("feishu_profile_json"),
            )
        )
        await uow.commit()
        logger.info(
            "飞书 SSO 建号成功 user_id=%s open_id=%s，将签发本系统会话并写入妙记 Token",
            user.id,
            open_id,
        )
        return user


async def bind_feishu_profile_to_user(
    user_id: int, profile: dict[str, Any]
) -> UserEntity:
    open_id = profile.get("feishu_open_id")
    if not open_id:
        raise ValueError("飞书资料缺少 open_id，无法绑定")
    now = utc_now_ms()
    async with UnitOfWork() as uow:
        assert uow.users is not None
        conflict = await uow.users.get_by_feishu_open_id(open_id)
        if conflict is not None and conflict.id != user_id:
            logger.error(
                "飞书 open_id 已被其他账号占用，拒绝绑定。open_id=%s target_user=%s owner_user=%s",
                open_id,
                user_id,
                conflict.id,
            )
            raise ValueError("该飞书账号已绑定其他平台用户，无法重复绑定")
        user = await uow.users.get_by_id(user_id)
        if user is None or user.status != "ACTIVE":
            raise ValueError("本地用户不存在或已停用")
        display_name = None
        if not (user.display_name or "").strip():
            display_name = profile.get("display_name_default") or user.username
        updated = await uow.users.update(
            UserUpdateEntity(
                id=user_id,
                updated_at=now,
                display_name=display_name,
                feishu_open_id=open_id,
                feishu_union_id=profile.get("feishu_union_id"),
                feishu_user_id=profile.get("feishu_user_id"),
                feishu_name=profile.get("feishu_name"),
                feishu_en_name=profile.get("feishu_en_name"),
                feishu_avatar_url=profile.get("feishu_avatar_url"),
                feishu_tenant_key=profile.get("feishu_tenant_key"),
                feishu_email=profile.get("feishu_email"),
                feishu_mobile=profile.get("feishu_mobile"),
                feishu_profile_json=profile.get("feishu_profile_json"),
            )
        )
        await uow.commit()
        if updated is None:
            raise ValueError("绑定飞书资料失败")
        logger.info(
            "飞书账号已绑定到本地用户 user_id=%s open_id=%s",
            user_id,
            open_id,
        )
        return updated
