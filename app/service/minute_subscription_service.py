"""用户级妙记变更订阅。

`minutes.minute.generated_v1` 除了要在开发者后台勾选事件，还必须用
user_access_token 调用一次订阅接口，否则长连接收不到推送。
"""

import logging

from app.integrations.feishu.minutes_client import FeishuMinutesClient
from app.integrations.feishu.user_auth import (
    FeishuUserAuthClient,
    UserAuthRequiredError,
)
from app.repository.uow import UnitOfWork

logger = logging.getLogger(__name__)


async def ensure_minute_generated_subscription(
    *,
    user_id: int | None = None,
    user_auth: FeishuUserAuthClient | None = None,
    minutes: FeishuMinutesClient | None = None,
) -> bool:
    """若指定用户（或全部已授权用户）已授权，则确保订阅了妙记生成事件。"""
    if user_auth is not None:
        auth = user_auth
        uid = auth.user_id
    elif user_id is not None:
        uid = int(user_id)
        auth = FeishuUserAuthClient(user_id=uid)
    else:
        return await ensure_all_users_minute_subscriptions()

    client = minutes or FeishuMinutesClient(user_id=uid)

    if not auth.is_authorized():
        logger.info("用户 %s 尚未完成飞书授权，跳过妙记生成事件订阅", uid)
        return False

    try:
        await client.subscribe_minute_generated()
    except UserAuthRequiredError:
        logger.warning(
            "用户 %s 授权已失效，无法订阅妙记生成事件，需要重新登录", uid
        )
        return False
    except Exception:
        logger.exception(
            "用户 %s 订阅妙记生成事件失败，怀疑权限未开通或应用未发布；"
            "此后该用户新会议不会自动下载，直到订阅成功",
            uid,
        )
        return False

    logger.info(
        "用户 %s 已订阅 minutes.minute.generated_v1，新妙记将自动下载并生成纪要",
        uid,
    )
    return True


async def ensure_all_users_minute_subscriptions() -> bool:
    """启动时：对所有已落库飞书 Token 的用户分别补订阅。"""
    async with UnitOfWork() as uow:
        assert uow.feishu_user_tokens is not None
        user_ids = await uow.feishu_user_tokens.list_user_ids_with_tokens()

    if not user_ids:
        logger.info("无已授权用户，跳过启动时妙记事件订阅")
        return False

    any_ok = False
    for uid in user_ids:
        ok = await ensure_minute_generated_subscription(user_id=uid)
        any_ok = any_ok or ok
    return any_ok
