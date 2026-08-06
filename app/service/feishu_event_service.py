import logging
from typing import Any

from app.core.config import settings
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
        self._download = download or MeetingDownloadService(self._storage)

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
        """妙记生成事件：对每位已授权用户分别用其本人 Token 尝试下载，绝不串用他人凭证。"""
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
        async with UnitOfWork() as uow:
            assert uow.feishu_user_tokens is not None
            user_ids = await uow.feishu_user_tokens.list_user_ids_with_tokens()

        if not user_ids:
            logger.warning(
                "收到妙记生成事件但无任何用户已授权飞书，跳过自动下载 token=%s",
                minute_token,
            )
            return []

        logger.info(
            "收到妙记生成事件 token=%s meeting_id=%s event_id=%s，将按 %s 位已授权用户分别下载",
            minute_token,
            meeting_id,
            event_id,
            len(user_ids),
        )

        record_ids: list[int] = []
        for user_id in user_ids:
            download = MeetingDownloadService(
                self._storage,
                FeishuMinutesClient(user_id=user_id),
            )
            result = await download.download_minute(
                minute_token,
                feishu_event_id=event_id,
                event_type=EVENT_MINUTE_GENERATED,
                unique_key=meeting_id,
                skip_if_completed=True,
                ready_retries=settings.minute_ready_max_attempts,
                ready_retry_seconds=settings.minute_ready_retry_seconds,
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

    @staticmethod
    def _extract_meeting_id(event: dict[str, Any]) -> str | None:
        source = event.get("minute_source")
        if not isinstance(source, dict):
            return None
        if source.get("source_type") != "meeting":
            return None
        entity_id = source.get("source_entity_id")
        return entity_id if isinstance(entity_id, str) and entity_id else None
