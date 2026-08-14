import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from app.integrations.feishu.minutes_client import FeishuMinutesClient
from app.repository.uow import UnitOfWork
from app.service.meeting_list_utils import create_time_sort_key
from app.service.meeting_storage_service import MeetingStorageService
from app.service.summary_event_broker import SummaryStatus, summary_broker

logger = logging.getLogger(__name__)

_SUMMARY_IN_FLIGHT = {SummaryStatus.QUEUED.value, SummaryStatus.GENERATING.value}


def _speakers_from_record(record) -> list[str]:
    raw = getattr(record, "speakers_json", None) if record else None
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(
            "会议 speakers_json 解析失败，人员筛选将忽略该会议；怀疑落库时写入了非法 JSON。"
            "raw=%s",
            raw[:200],
        )
        return []
    if not isinstance(data, list):
        return []
    return [str(x).strip() for x in data if str(x).strip()]


class MeetingListService:
    """云端妙记列表 + 本地持久化状态。"""

    def __init__(
        self,
        minutes_client: FeishuMinutesClient | None = None,
        storage: MeetingStorageService | None = None,
    ) -> None:
        self._minutes = minutes_client
        self._storage = storage or MeetingStorageService()

    def _minutes_for(self, user_id: int) -> FeishuMinutesClient:
        return self._minutes or FeishuMinutesClient(user_id=user_id)

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
        owner_user_id: int | None = None,
    ) -> dict[str, Any]:
        if owner_user_id is None:
            raise RuntimeError("云端妙记列表必须指定 owner_user_id（当前登录用户）")
        minutes = self._minutes_for(owner_user_id)
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

        search_result = await minutes.search_minutes(
            query=keyword or query,
            start_time=resolved_start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            end_time=resolved_end.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            page_size=page_size,
            page_token=page_token,
        )

        tokens = [item.token for item in search_result.items if item.token]
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            # 普通用户只看自己名下记录；超管(owner_user_id=None)可看任意归属的最新记录
            record_map = await uow.meeting_records.map_latest_by_minute_tokens(
                tokens, owner_user_id=owner_user_id
            )

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
            # 仅当本用户有库记录时才算「已本地」；磁盘上他人文件不算自己的
            owned = record is not None and (
                owner_user_id is None or record.owner_user_id == owner_user_id
            )
            local_status = "NONE"
            record_id = None
            if owned and record:
                record_id = record.id
                local_status = record.status

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
                    "is_local": local_status == "COMPLETED",
                    "local_status": local_status,
                    "record_id": record_id,
                    "summary_status": (
                        self._summary_status(
                            item.token,
                            owner_user_id=owner_user_id,
                            db_summary_status=record.summary_status if record else None,
                        )
                        if owned
                        else "NONE"
                    ),
                    "speakers": _speakers_from_record(record) if owned else [],
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

    def _summary_status(
        self,
        minute_token: str,
        *,
        owner_user_id: int,
        db_summary_status: str | None = None,
    ) -> str:
        """列表纪要状态：broker 进行中优先，其余只信 DB，禁止扫盘。"""
        channel = summary_broker.get(minute_token, owner_user_id=owner_user_id)
        if channel is not None and channel.status in _SUMMARY_IN_FLIGHT:
            return channel.status
        db_status = (db_summary_status or "").strip().upper()
        if db_status in {"COMPLETED", "READY"}:
            return "READY"
        if db_status in _SUMMARY_IN_FLIGHT:
            return db_status
        if channel is not None and channel.status == SummaryStatus.FAILED.value:
            return SummaryStatus.FAILED.value
        if db_status == SummaryStatus.FAILED.value:
            return SummaryStatus.FAILED.value
        return "NONE"

    async def list_stored_meetings(
        self,
        *,
        owner_user_id: int | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """从 DB 列出已入库会议。owner_user_id=None 表示超管看全站。"""
        async with UnitOfWork() as uow:
            assert uow.meeting_records is not None
            assert uow.users is not None
            records = await uow.meeting_records.list_latest_by_scope(
                owner_user_id=owner_user_id, limit=limit
            )
            owner_ids = {
                r.owner_user_id for r in records if r.owner_user_id is not None
            }
            usernames: dict[int, str] = {}
            for oid in owner_ids:
                user = await uow.users.get_by_id(oid)
                if user is not None and user.id is not None:
                    usernames[user.id] = user.public_display_name()

        items: list[dict[str, Any]] = []
        for record in records:
            token = (record.minute_token or "").strip()
            if not token:
                continue
            create_time = None
            if record.create_time and str(record.create_time).strip():
                create_time = str(record.create_time).strip()
            elif record.downloaded_at is not None:
                create_time = datetime.fromtimestamp(
                    record.downloaded_at / 1000.0
                    if record.downloaded_at > 10_000_000_000
                    else float(record.downloaded_at),
                    tz=timezone.utc,
                ).isoformat()
            elif record.created_at is not None:
                dt = record.created_at
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                create_time = dt.isoformat()

            account_name = (
                usernames.get(record.owner_user_id)
                if record.owner_user_id is not None
                else None
            )
            row_owner = record.owner_user_id
            if row_owner is not None:
                summary_status = self._summary_status(
                    token,
                    owner_user_id=row_owner,
                    db_summary_status=record.summary_status,
                )
            else:
                summary_status = "NONE"
            items.append(
                {
                    "minute_token": token,
                    "display_info": record.title,
                    "title": record.title,
                    "create_time": create_time,
                    "duration_ms": record.duration_ms,
                    "cover": None,
                    "app_link": None,
                    "description": None,
                    "url": None,
                    "owner_id": str(record.owner_user_id)
                    if record.owner_user_id is not None
                    else None,
                    "note_id": None,
                    "keywords": None,
                    "owner_name": account_name,
                    "duration_text": None,
                    "is_local": record.status == "COMPLETED",
                    "local_status": record.status or "NONE",
                    "record_id": record.id,
                    "summary_status": summary_status,
                    "speakers": _speakers_from_record(record),
                }
            )

        items.sort(key=lambda row: create_time_sort_key(row.get("create_time")), reverse=True)
        return {
            "items": items,
            "total": len(items),
            "filtered_total": len(items),
            "has_more": False,
            "page_token": None,
        }

    def _decorate_local_meeting(
        self, minute_token: str, detail: dict[str, Any]
    ) -> dict[str, Any]:
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
            "has_transcript": bool(detail.get("has_transcript")),
            "downloaded_at": detail.get("meta", {}).get("downloaded_at"),
        }

    async def get_local_meeting_async(
        self, minute_token: str, *, owner_user_id: int
    ) -> dict[str, Any] | None:
        detail = await self._storage.get_local_detail_async(
            minute_token, owner_user_id=owner_user_id
        )
        if detail is None:
            return None
        return self._decorate_local_meeting(minute_token, detail)

    def get_local_meeting(
        self, minute_token: str, *, owner_user_id: int
    ) -> dict[str, Any] | None:
        detail = self._storage.get_local_detail(
            minute_token, owner_user_id=owner_user_id
        )
        if detail is None:
            return None
        return self._decorate_local_meeting(minute_token, detail)
