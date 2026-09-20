"""每天串行扫一遍库里的飞书 refresh，临期两天内就刷。

页面打开和调妙记 API 也会顺手刷，但这条巡检保证没人登录的账号也能把
refresh 滑下去，不会静静放到过期。
"""

from __future__ import annotations

import asyncio
import logging

from app.integrations.feishu.user_auth import (
    FeishuUserAuthClient,
    refresh_token_is_due,
)
from app.repository.uow import UnitOfWork

logger = logging.getLogger(__name__)

DAILY_INTERVAL_SECONDS = 24 * 3600


def pick_due_refresh_user_ids(
    rows: list,
    *,
    now: float | None = None,
) -> list[int]:
    """有 refresh、且到期进入两天窗口（或旧行没记到期）的用户。"""
    due: list[int] = []
    seen: set[int] = set()
    for row in rows:
        user_id = getattr(row, "user_id", None)
        refresh = (getattr(row, "refresh_token", None) or "").strip()
        if user_id is None or not refresh:
            continue
        uid = int(user_id)
        if uid in seen:
            continue
        if not refresh_token_is_due(
            getattr(row, "refresh_expires_at", None),
            now=now,
            unknown_is_due=True,
        ):
            continue
        seen.add(uid)
        due.append(uid)
    return due


async def refresh_due_feishu_tokens() -> int:
    async with UnitOfWork() as uow:
        assert uow.feishu_user_tokens is not None
        rows = await uow.feishu_user_tokens.list_all()
    due = pick_due_refresh_user_ids(rows)
    if not due:
        logger.info("飞书 refresh 日巡：%d 个账号，无需续期", len(rows))
        return 0

    refreshed = 0
    for user_id in due:
        try:
            await FeishuUserAuthClient(user_id=user_id).refresh_access_token()
            refreshed += 1
            logger.info("飞书 refresh 日巡：已续期 user_id=%s", user_id)
        except Exception:
            logger.warning(
                "飞书 refresh 日巡失败 user_id=%s，留给下次或用户重新授权",
                user_id,
                exc_info=True,
            )
    logger.info(
        "飞书 refresh 日巡结束：扫描 %d，临期 %d，成功 %d",
        len(rows),
        len(due),
        refreshed,
    )
    return refreshed


async def run_daily_feishu_token_refresh_loop() -> None:
    while True:
        try:
            await refresh_due_feishu_tokens()
        except Exception:
            logger.exception("飞书 refresh 日巡整轮失败，%s 秒后再试", DAILY_INTERVAL_SECONDS)
        await asyncio.sleep(DAILY_INTERVAL_SECONDS)
