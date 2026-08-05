import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from app.core.config import settings
from app.integrations.feishu.minutes_client import FeishuMinutesClient
from app.repository.uow import UnitOfWork
from app.service.meeting_list_utils import create_time_sort_key
from app.service.meeting_storage_service import MeetingStorageService
from app.service.summary_event_broker import SummaryStatus, summary_broker

logger = logging.getLogger(__name__)

_SUMMARY_IN_FLIGHT = {SummaryStatus.QUEUED.value, SummaryStatus.GENERATING.value}


class MeetingListService:
    """云端妙记列表 + 本地持久化状态。"""

    def __init__(
        self,
        minutes_client: FeishuMinutesClient | None = None,
        storage: MeetingStorageService | None = None,
    ) -> None:
        self._minutes = minutes_client or FeishuMinutesClient()
        self._storage = storage or MeetingStorageService()

    async def list_cloud_meetings(
        self,
        *,
        query: str | None = None,
        keyword: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        days: int | None = None,
        min_duration_ms: int | None = None,
        max_duration_ms: int | None = None,
        page_size: int = 20,
        page_token: str | None = None,
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        resolved_start = start_time
        resolved_end = end_time
        if resolved_start is None and resolved_end is None:
            span_days = max(days or 30, 1)
            resolved_start = now - timedelta(days=span_days)
            resolved_end = now
        elif resolved_start is None:
            resolved_start = (resolved_end or now) - timedelta(days=30)
        elif resolved_end is None:
            resolved_end = now

        if resolved_start is None or resolved_end is None:
            raise RuntimeError("时间范围解析失败")

        if resolved_start.tzinfo is None:
            resolved_start = resolved_start.replace(tzinfo=timezone.utc)
        if resolved_end.tzinfo is None:
            resolved_end = resolved_end.replace(tzinfo=timezone.utc)

        search_result = await self._minutes.search_minutes(
            query=keyword or query,
            start_time=resolved_start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            end_time=resolved_end.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            page_size=page_size,
            page_token=page_token,
        )

        tokens = [item.token for item in search_result.items if item.token]
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            record_map = await uow.meeting_records.map_latest_by_minute_tokens(tokens)

        items = []
        for item in search_result.items:
            if not item.token:
                continue
            if min_duration_ms is not None and (
                item.duration_ms is None or item.duration_ms < min_duration_ms
            ):
                continue
            if max_duration_ms is not None and (
                item.duration_ms is None or item.duration_ms > max_duration_ms
            ):
                continue

            record = record_map.get(item.token)
            persisted = self._storage.is_persisted(item.token)
            local_status = "NONE"
            record_id = None
            if record:
                record_id = record.id
                local_status = record.status
            elif persisted:
                local_status = "COMPLETED"

            items.append(
                {
                    "minute_token": item.token,
                    "display_info": item.display_info,
                    "title": item.title,
                    "create_time": item.create_time,
                    "duration_ms": item.duration_ms,
                    "cover": item.cover,
                    "app_link": item.app_link,
                    "description": item.description,
                    "url": item.url,
                    "owner_id": item.owner_id,
                    "note_id": item.note_id,
                    "keywords": item.keywords,
                    "owner_name": item.owner_name,
                    "duration_text": item.duration_text,
                    "is_local": persisted or local_status == "COMPLETED",
                    "local_status": local_status,
                    "record_id": record_id,
                    "summary_status": self._summary_status(item.token),
                }
            )

        reverse = sort_order.lower() != "asc"
        items.sort(key=lambda row: create_time_sort_key(row.get("create_time")), reverse=reverse)

        return {
            "items": items,
            "total": len(items) if search_result.total == 0 else search_result.total,
            "filtered_total": len(items),
            "has_more": search_result.has_more,
            "page_token": search_result.page_token,
        }

    def _summary_status(self, minute_token: str) -> str:
        """列表上展示的纪要状态：进行中的生成优先于磁盘上的旧纪要。"""
        channel = summary_broker.get(minute_token)
        if channel is not None and channel.status in _SUMMARY_IN_FLIGHT:
            return channel.status
        if self._storage.has_summary(minute_token):
            return "READY"
        if channel is not None and channel.status == SummaryStatus.FAILED.value:
            return SummaryStatus.FAILED.value
        return "NONE"

    def get_local_meeting(self, minute_token: str) -> dict[str, Any] | None:
        detail = self._storage.get_local_detail(minute_token)
        if detail is None:
            return None
        media_files = []
        for mf in detail.get("media_files", []):
            media_files.append(
                {
                    "name": mf["name"],
                    "kind": mf["kind"],
                    # 相对路径：由前端按当前 apiBase 拼接，避免管理端误打公网 Tunnel/R2
                    "url": (
                        f"/api/v1/meetings/local/{minute_token}/media/"
                        f"{quote(mf['name'], safe='')}"
                    ),
                }
            )
        return {
            "minute_token": minute_token,
            "title": detail.get("title"),
            "duration_ms": detail.get("duration_ms"),
            "has_video": detail.get("has_video"),
            "media_files": media_files,
            "transcript": detail.get("transcript"),
            "downloaded_at": detail.get("meta", {}).get("downloaded_at"),
        }
