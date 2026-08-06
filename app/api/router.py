from fastapi import APIRouter

from app.api.routes import (
    access_keys,
    admin_users,
    auth,
    auth_admin,
    exports,
    feishu,
    meetings,
    shares,
    summaries,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_admin.router)
api_router.include_router(auth.router)
api_router.include_router(feishu.router, tags=["feishu"])
api_router.include_router(access_keys.router)
api_router.include_router(admin_users.router)
api_router.include_router(shares.guest_router)
# meetings 在前：/meetings/local/{token} 需先于 /meetings/{token}/summary 参与匹配
api_router.include_router(meetings.router)
api_router.include_router(shares.admin_router)
api_router.include_router(exports.router)
api_router.include_router(summaries.router)
