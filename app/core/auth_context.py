"""从 Request 读取 JWT 鉴权主体（中间件写入 request.state.auth）。

禁止使用 ContextVar 传递用户身份：身份只存在 JWT 与请求对象上，
由 Router 解析后显式向下传递 user_id / AuthPrincipal。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.core.jwt_auth import AuthPrincipal


def get_request_auth(request: Request) -> AuthPrincipal | None:
    return getattr(request.state, "auth", None)


def require_auth(request: Request) -> AuthPrincipal:
    auth = get_request_auth(request)
    if auth is None:
        raise HTTPException(status_code=401, detail="需要登录")
    return auth


def require_user_id(request: Request) -> int:
    """写接口：仅普通用户 JWT。"""
    auth = require_auth(request)
    if not auth.is_user or auth.user_id is None:
        raise HTTPException(
            status_code=403,
            detail="超级管理员模式仅可只读，请切换回管理端后再执行写操作",
        )
    return auth.user_id


def owner_scope_user_id(request: Request) -> int | None:
    """读接口：USER 返回自身 id 用于过滤；SUPER_ADMIN 返回 None（不过滤）。"""
    auth = require_auth(request)
    if auth.is_super_admin:
        return None
    if auth.user_id is None:
        raise HTTPException(status_code=401, detail="需要登录")
    return auth.user_id


def require_super_admin(request: Request) -> AuthPrincipal:
    auth = require_auth(request)
    if not auth.is_super_admin:
        raise HTTPException(status_code=403, detail="需要超级管理员权限")
    return auth
