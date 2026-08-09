import logging
from typing import Any

from app.core import runtime_config
from app.integrations.feishu.events import EVENT_MINUTE_GENERATED
from app.integrations.feishu.minutes_client import FeishuMinutesClient
from app.repository.uow import UnitOfWork
from app.service.meeting_download_service import MeetingDownloadService
from app.service.meeting_storage_service import MeetingStorageService

logger = logging.getLogger(__name__)


class FeishuEventService:
    """处理飞书长连接推送的事件。"""

    def __init__(
        self,
        storage: MeetingStorageService | None = None,
        download: MeetingDownloadService | None = None,
    ) -> None:
        self._storage = storage or MeetingStorageService()
        # 测试可注入假下载器；生产路径按用户新建带本人 Token 的客户端
        self._download_override = download

    @staticmethod
    def _parse_header(payload: dict[str, Any]) -> dict[str, Any]:
        header = payload.get("header", {})
        if not isinstance(header, dict):
            return {}
        return header

    @staticmethod
    def _parse_event_body(payload: dict[str, Any]) -> dict[str, Any]:
        event = payload.get("event", {})
        if not isinstance(event, dict):
            return {}
        return event

    async def handle_event(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        header = self._parse_header(payload)
        event_type = header.get("event_type", "")
        event_id = header.get("event_id", "")

        if not event_type:
            logger.warning("收到无 event_type 的飞书事件，无法路由处理")
            return None

        if event_type == EVENT_MINUTE_GENERATED:
            record_ids = await self._handle_minute_generated(payload)
            return {
                "handled": True,
                "event_type": event_type,
                "record_ids": record_ids,
            }

        logger.info("忽略未处理的事件类型: %s event_id=%s", event_type, event_id)
        return None

    async def _handle_minute_generated(self, payload: dict[str, Any]) -> list[int]:
        """妙记生成事件：仅对事件 subscriber_ids 中、且本系统已授权的用户下载。"""
        header = self._parse_header(payload)
        event = self._parse_event_body(payload)
        event_id = header.get("event_id", "")
        if not event_id:
            logger.error("minute.generated 缺少 event_id，无法幂等处理")
            return []

        minute_token = event.get("minute_token")
        if not isinstance(minute_token, str) or not minute_token.strip():
            logger.error(
                "minute.generated 缺少 minute_token event_id=%s，怀疑事件体结构变化",
                event_id,
            )
            return []
        minute_token = minute_token.strip()

        meeting_id = self._extract_meeting_id(event)
        user_ids = await self._resolve_subscriber_user_ids(event)
        if not user_ids:
            logger.warning(
                "妙记事件无本系统可下载用户（subscriber_ids 为空或未绑定本地账号）"
                " token=%s event_id=%s，跳过自动下载",
                minute_token,
                event_id,
            )
            return []

        logger.info(
            "收到妙记生成事件 token=%s meeting_id=%s event_id=%s，"
            "将按 subscriber 映射的 %s 位本地用户分别下载",
            minute_token,
            meeting_id,
            event_id,
            len(user_ids),
        )

        record_ids: list[int] = []
        for user_id in user_ids:
            download = self._download_override or MeetingDownloadService(
                self._storage,
                FeishuMinutesClient(user_id=user_id),
            )
            result = await download.download_minute(
                minute_token,
                feishu_event_id=event_id,
                event_type=EVENT_MINUTE_GENERATED,
                unique_key=meeting_id,
                skip_if_completed=True,
                ready_retries=runtime_config.get_int("MINUTE_READY_MAX_ATTEMPTS", 8),
                ready_retry_seconds=runtime_config.get_float(
                    "MINUTE_READY_RETRY_SECONDS", 15.0
                ),
                owner_user_id=user_id,
            )

            if result.status == "FAILED":
                logger.info(
                    "用户 %s 无法下载妙记 token=%s（可能无飞书侧权限）: %s",
                    user_id,
                    minute_token,
                    result.error_message,
                )
                continue
            if result.record_id is not None:
                record_ids.append(result.record_id)
        return record_ids

    @classmethod
    async def _resolve_subscriber_user_ids(cls, event: dict[str, Any]) -> list[int]:
        """用事件 subscriber_ids.open_id 映射本地已授权用户；无列表则不下载。"""
        open_ids = cls._extract_subscriber_open_ids(event)
        if not open_ids:
            return []

        async with UnitOfWork() as uow:
            assert uow.users is not None
            assert uow.feishu_user_tokens is not None
            token_users = set(await uow.feishu_user_tokens.list_user_ids_with_tokens())
            if not token_users:
                return []

            resolved: list[int] = []
            seen: set[int] = set()
            for open_id in open_ids:
                user = await uow.users.get_by_feishu_open_id(open_id)
                if user is None or user.id is None:
                    logger.info(
                        "subscriber open_id=%s 未绑定本系统用户，跳过下载",
                        open_id,
                    )
                    continue
                if user.id not in token_users:
                    logger.info(
                        "subscriber 用户 %s 无可用飞书 Token，跳过下载",
                        user.id,
                    )
                    continue
                if user.id in seen:
                    continue
                seen.add(user.id)
                resolved.append(user.id)
            return resolved

    @staticmethod
    def _extract_subscriber_open_ids(event: dict[str, Any]) -> list[str]:
        raw = event.get("subscriber_ids")
        if not isinstance(raw, list):
            return []
        open_ids: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            open_id = item.get("open_id")
            if not isinstance(open_id, str):
                continue
            open_id = open_id.strip()
            if not open_id or open_id in seen:
                continue
            seen.add(open_id)
            open_ids.append(open_id)
        return open_ids

    @staticmethod
    def _extract_meeting_id(event: dict[str, Any]) -> str | None:
        source = event.get("minute_source")
        if not isinstance(source, dict):
            return None
        if source.get("source_type") != "meeting":
            return None
        entity_id = source.get("source_entity_id")
        return entity_id if isinstance(entity_id, str) and entity_id else None
