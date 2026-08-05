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

logger = logging.getLogger(__name__)


async def ensure_minute_generated_subscription(
    *,
    user_auth: FeishuUserAuthClient | None = None,
    minutes: FeishuMinutesClient | None = None,
) -> bool:
    """若本地已有用户授权，则确保订阅了妙记生成事件。成功返回 True。"""
    auth = user_auth or FeishuUserAuthClient()
    client = minutes or FeishuMinutesClient()

    if not auth.is_authorized():
        logger.info("尚未完成飞书用户授权，跳过妙记生成事件订阅")
        return False

    try:
        await client.subscribe_minute_generated()
    except UserAuthRequiredError:
        logger.warning("用户授权已失效，无法订阅妙记生成事件，需要重新登录")
        return False
    except Exception:
        logger.exception(
            "订阅妙记生成事件失败，怀疑权限未开通或应用未发布；"
            "此后新会议不会自动下载，直到订阅成功"
        )
        return False

    logger.info("已订阅 minutes.minute.generated_v1，新妙记将自动下载并生成纪要")
    return True
