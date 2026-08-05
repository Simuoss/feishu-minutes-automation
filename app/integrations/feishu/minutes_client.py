import asyncio
import html
import logging
import re
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlparse

from app.integrations.feishu.client import FeishuApiClient
from app.integrations.feishu.events import EVENT_MINUTE_GENERATED

logger = logging.getLogger(__name__)

MINUTE_TOKEN_PATTERN = re.compile(r"obcn[a-z0-9]{20}", re.IGNORECASE)
DESCRIPTION_META_PATTERN = re.compile(
    r"所有者:\s*(?P<owner>.+?)\s+开始时间:\s*(?P<start>.+?)\s+时长:\s*(?P<duration>.+)$"
)
KEYWORDS_PATTERN = re.compile(r"关键词:\s*(.+)")


@dataclass
class MinuteSearchItem:
    token: str
    display_info: str | None
    title: str | None
    create_time: str | None
    duration_ms: int | None
    cover: str | None
    app_link: str | None
    description: str | None
    url: str | None
    owner_id: str | None
    note_id: int | None
    keywords: str | None = None
    owner_name: str | None = None
    duration_text: str | None = None


@dataclass
class MinuteSearchResult:
    items: list[MinuteSearchItem]
    total: int
    has_more: bool
    page_token: str | None


@dataclass
class MinuteInfo:
    token: str
    title: str | None
    create_time: str | None
    duration_ms: int | None
    url: str | None
    note_id: int | None
    cover: str | None
    owner_id: str | None


@dataclass
class MeetingRecordingInfo:
    meeting_id: str
    minute_token: str | None
    recording_url: str | None
    duration_ms: int | None


class FeishuMinutesClient:
    """妙记 API 封装。

    - 获取妙记信息：https://open.feishu.cn/document/server-docs/minutes-v1/minute/get
    - 下载音视频：https://open.feishu.cn/document/minutes-v1/minute-media/get
    - 导出转写：https://open.feishu.cn/document/minutes-v1/minute-transcript/get
    """

    def __init__(self, api: FeishuApiClient | None = None) -> None:
        self._api = api or FeishuApiClient()

    @staticmethod
    def extract_minute_token(value: str | None) -> str | None:
        if not value:
            return None
        match = MINUTE_TOKEN_PATTERN.search(value)
        return match.group(0) if match else None

    @staticmethod
    def extract_minute_token_from_url(url: str | None) -> str | None:
        if not url:
            return None
        path = urlparse(url).path.rstrip("/")
        last_segment = path.split("/")[-1] if path else ""
        return FeishuMinutesClient.extract_minute_token(last_segment)

    async def get_minute_info(
        self, minute_token: str, *, use_user_token: bool = False
    ) -> MinuteInfo:
        data = await self._api.request(
            "GET",
            f"/open-apis/minutes/v1/minutes/{minute_token}",
            use_user_token=use_user_token,
        )
        minute: dict[str, Any] = data.get("minute", {})
        duration_raw = minute.get("duration")
        duration_ms = int(duration_raw) if duration_raw else None
        note_id_raw = minute.get("note_id")
        note_id = int(note_id_raw) if note_id_raw is not None else None
        return MinuteInfo(
            token=minute.get("token", minute_token),
            title=minute.get("title"),
            create_time=minute.get("create_time"),
            duration_ms=duration_ms,
            url=minute.get("url"),
            note_id=note_id,
            cover=minute.get("cover"),
            owner_id=minute.get("owner_id"),
        )

    async def search_minutes(
        self,
        *,
        query: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int = 20,
        page_token: str | None = None,
        enrich: bool = False,
    ) -> MinuteSearchResult:
        """搜索妙记列表。

        文档：https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/minutes-v1/minute/search

        enrich=True 时会为每条结果额外请求 GET /minutes/{token} 补全详情，
        一页 20 条即 N+1 次飞书 API，易触发限流且显著拖慢列表（通常 >15s）。
        列表展示所需字段已由 search 返回的 display_info / description 解析得出。
        """
        # page_size / page_token 必须走 query；放进 body 时飞书会忽略分页，
        # 一直返回 has_more=true + 同一页数据，前端全量同步会把十几条吹成几百条。
        params: dict[str, Any] = {"page_size": min(max(page_size, 1), 30)}
        if page_token:
            params["page_token"] = page_token

        body: dict[str, Any] = {}
        if query:
            body["query"] = query

        time_filter: dict[str, str] = {}
        if start_time:
            time_filter["start_time"] = start_time
        if end_time:
            time_filter["end_time"] = end_time
        if time_filter:
            body["filter"] = {"create_time": time_filter}

        if "query" not in body and "filter" not in body:
            raise ValueError("搜索妙记至少需要 query 或时间范围过滤条件")

        data = await self._api.request(
            "POST",
            "/open-apis/minutes/v1/minutes/search",
            params=params,
            json_body=body,
            use_user_token=True,
        )
        items = [self._normalize_search_item(self._parse_search_item(raw)) for raw in data.get("items", [])]
        if items and enrich:
            items = await self._enrich_search_items(items)

        total_raw = data.get("total")
        total = int(total_raw) if total_raw is not None else len(items)
        if total == 0 and items:
            total = len(items)

        return MinuteSearchResult(
            items=items,
            total=total,
            has_more=bool(data.get("has_more")),
            page_token=data.get("page_token"),
        )

    def _parse_search_item(self, raw: dict[str, Any]) -> MinuteSearchItem:
        meta = raw.get("meta_data") or {}
        return MinuteSearchItem(
            token=raw.get("token", ""),
            display_info=raw.get("display_info"),
            title=None,
            create_time=None,
            duration_ms=None,
            cover=meta.get("avatar"),
            app_link=meta.get("app_link"),
            description=meta.get("description"),
            url=None,
            owner_id=None,
            note_id=None,
        )

    @staticmethod
    def _strip_display_html(text: str) -> str:
        plain = html.unescape(text)
        plain = re.sub(r"<[^>]+>", "", plain)
        return plain.strip()

    def _normalize_search_item(self, item: MinuteSearchItem) -> MinuteSearchItem:
        display_plain = self._strip_display_html(item.display_info) if item.display_info else ""
        lines = [line.strip() for line in display_plain.splitlines() if line.strip()]
        title = lines[0] if lines else item.title

        keywords = None
        for line in lines[1:]:
            match = KEYWORDS_PATTERN.search(line)
            if match:
                keywords = match.group(1).strip()
                break

        owner_name = None
        start_time = item.create_time
        duration_text = item.duration_text
        meta_source = item.description or display_plain
        meta_match = DESCRIPTION_META_PATTERN.search(meta_source.replace("\n", " "))
        if meta_match:
            owner_name = meta_match.group("owner").strip()
            start_time = start_time or meta_match.group("start").strip()
            duration_text = duration_text or meta_match.group("duration").strip()

        url = item.url or item.app_link
        duration_ms = item.duration_ms or self._parse_duration_text(duration_text)
        return replace(
            item,
            title=title or item.token,
            create_time=start_time,
            duration_ms=duration_ms,
            keywords=keywords,
            owner_name=owner_name,
            duration_text=duration_text,
            url=url,
        )

    @staticmethod
    def _parse_duration_text(text: str | None) -> int | None:
        if not text:
            return None
        hours = minutes = seconds = 0
        if match := re.search(r"(\d+)\s*小时", text):
            hours = int(match.group(1))
        if match := re.search(r"(\d+)\s*分", text):
            minutes = int(match.group(1))
        if match := re.search(r"(\d+)\s*秒", text):
            seconds = int(match.group(1))
        total_ms = (hours * 3600 + minutes * 60 + seconds) * 1000
        return total_ms if total_ms > 0 else None

    async def _enrich_search_items(self, items: list[MinuteSearchItem]) -> list[MinuteSearchItem]:
        enriched = await asyncio.gather(
            *[self._enrich_search_item(item) for item in items],
            return_exceptions=True,
        )
        result: list[MinuteSearchItem] = []
        for item, detail in zip(items, enriched, strict=True):
            if isinstance(detail, BaseException):
                logger.warning(
                    "补全妙记详情失败 token=%s err=%s，列表将仅展示 search 返回字段",
                    item.token,
                    detail,
                )
                result.append(self._normalize_search_item(replace(item, title=item.display_info or item.title)))
            else:
                result.append(detail)
        return result

    async def _enrich_search_item(self, item: MinuteSearchItem) -> MinuteSearchItem:
        if not item.token:
            return item
        info = await self.get_minute_info(item.token, use_user_token=True)
        return self._normalize_search_item(
            replace(
                item,
                title=info.title or item.title,
                create_time=info.create_time or item.create_time,
                duration_ms=info.duration_ms if info.duration_ms is not None else item.duration_ms,
                cover=info.cover or item.cover,
                url=info.url or item.url or item.app_link,
                owner_id=info.owner_id or item.owner_id,
                note_id=info.note_id if info.note_id is not None else item.note_id,
            )
        )

    async def get_media_download_url(
        self, minute_token: str, *, use_user_token: bool = False
    ) -> str:
        data = await self._api.request(
            "GET",
            f"/open-apis/minutes/v1/minutes/{minute_token}/media",
            use_user_token=use_user_token,
        )
        download_url = data.get("download_url")
        if not download_url:
            raise RuntimeError(f"妙记 {minute_token} 未返回媒体下载链接，怀疑转写尚未完成")
        return download_url

    async def subscribe_minute_generated(self) -> None:
        """以当前用户身份订阅妙记生成事件。

        文档：https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/minutes-v1/minute/subscription
        仅在开发者后台勾选事件不够，还必须用 user_access_token 调一次本接口。
        """
        await self._api.request(
            "POST",
            "/open-apis/minutes/v1/minutes/subscription",
            json_body={"event_type": EVENT_MINUTE_GENERATED},
            use_user_token=True,
        )

    async def download_transcript(
        self,
        minute_token: str,
        *,
        file_format: str = "txt",
        need_speaker: bool = True,
        need_timestamp: bool = True,
        use_user_token: bool = False,
    ) -> bytes:
        import httpx

        from app.core.config import settings

        params = {
            "need_speaker": str(need_speaker).lower(),
            "need_timestamp": str(need_timestamp).lower(),
            "file_format": file_format,
        }
        if use_user_token:
            token = await self._api._user_auth.get_user_access_token()
        else:
            token = await self._api._auth.get_tenant_access_token()
        api_url = (
            f"{settings.feishu_api_base}/open-apis/minutes/v1/minutes/{minute_token}/transcript"
        )
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(api_url, headers=headers, params=params)
            if response.status_code != 200:
                logger.error(
                    "导出妙记转写失败 token=%s status=%s，怀疑转写未完成或无导出权限",
                    minute_token,
                    response.status_code,
                )
                raise RuntimeError(f"导出转写失败: HTTP {response.status_code}")
            return response.content


class FeishuVcClient:
    """视频会议 API 封装。

    - 获取录制文件：https://open.feishu.cn/document/server-docs/vc-v1/meeting-recording/get
    """

    def __init__(self, api: FeishuApiClient | None = None) -> None:
        self._api = api or FeishuApiClient()
        self._minutes = FeishuMinutesClient(self._api)

    async def get_meeting_recording(self, meeting_id: str) -> MeetingRecordingInfo:
        data = await self._api.request(
            "GET", f"/open-apis/vc/v1/meetings/{meeting_id}/recording"
        )
        recording: dict[str, Any] = data.get("recording", {})
        url = recording.get("url")
        duration_raw = recording.get("duration")
        duration_ms = int(duration_raw) if duration_raw else None
        minute_token = self._minutes.extract_minute_token_from_url(url)
        return MeetingRecordingInfo(
            meeting_id=meeting_id,
            minute_token=minute_token,
            recording_url=url,
            duration_ms=duration_ms,
        )
