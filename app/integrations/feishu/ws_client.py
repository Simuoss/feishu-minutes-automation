import asyncio
import json
import logging
import threading
from typing import Any

import lark_oapi as lark
from lark_oapi import LogLevel, ws
from lark_oapi.api.minutes.v1.model.p2_minutes_minute_generated_v1 import (
    P2MinutesMinuteGeneratedV1,
)

from app.core.config import settings
from app.integrations.feishu.events import EVENT_MINUTE_GENERATED
from app.service.feishu_event_service import FeishuEventService

logger = logging.getLogger(__name__)

_event_service = FeishuEventService()


def _event_to_payload(data: Any) -> dict[str, Any]:
    raw = lark.JSON.marshal(data)
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("事件序列化结果不是 JSON 对象")
    return payload


def _schedule_handle(payload: dict[str, Any]) -> None:
    """在独立线程中异步处理事件，满足长连接 3 秒内响应要求。"""

    def _run() -> None:
        try:
            asyncio.run(_event_service.handle_event(payload))
        except Exception:
            logger.exception("后台处理飞书事件失败，怀疑下载链路或权限配置异常")

    threading.Thread(target=_run, daemon=True, name="feishu-event-worker").start()


def _handle_minute_generated(data: P2MinutesMinuteGeneratedV1) -> None:
    payload = _event_to_payload(data)
    event_id = payload.get("header", {}).get("event_id", "unknown")
    minute_token = payload.get("event", {}).get("minute_token", "")
    logger.info(
        "长连接收到事件 type=%s id=%s token=%s",
        EVENT_MINUTE_GENERATED,
        event_id,
        minute_token,
    )
    _schedule_handle(payload)


def build_event_handler() -> lark.EventDispatcherHandler:
    """构建飞书长连接事件分发器。

    文档：https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events
    长连接模式下 encrypt_key / verification_token 必须为空字符串。
    """
    builder = lark.EventDispatcherHandler.builder("", "").register_p2_minutes_minute_generated_v1(
        _handle_minute_generated
    )
    return builder.build()


def build_ws_client() -> ws.Client:
    log_level = LogLevel.DEBUG if settings.app_debug else LogLevel.INFO
    return ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=build_event_handler(),
        log_level=log_level,
    )


class FeishuWsClientRunner:
    """在后台线程运行飞书 WebSocket 长连接客户端。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.warning("飞书长连接已在运行，跳过重复启动")
            return
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="feishu-ws-client",
        )
        self._thread.start()
        logger.info("飞书长连接客户端已在后台启动")

    @staticmethod
    def _run() -> None:
        try:
            client = build_ws_client()
            client.start()
        except Exception:
            logger.exception("飞书长连接客户端异常退出，怀疑网络不可达或 App 凭证无效")
