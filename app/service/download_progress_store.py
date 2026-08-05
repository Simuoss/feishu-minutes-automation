from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class DownloadProgress:
    minute_token: str
    percent: int = 0
    stage: str = "等待中"
    status: str = "pending"
    error_message: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class DownloadProgressStore:
    """内存中的单会议下载进度（进程内有效）。"""

    def __init__(self) -> None:
        self._items: dict[str, DownloadProgress] = {}

    def start(self, minute_token: str) -> None:
        self._items[minute_token] = DownloadProgress(
            minute_token=minute_token,
            percent=0,
            stage="准备下载",
            status="downloading",
        )

    def update(self, minute_token: str, *, percent: int, stage: str) -> None:
        item = self._items.get(minute_token)
        if item is None:
            item = DownloadProgress(minute_token=minute_token, status="downloading")
            self._items[minute_token] = item
        item.percent = max(0, min(100, percent))
        item.stage = stage
        item.status = "downloading"
        item.updated_at = datetime.now(timezone.utc).isoformat()

    def complete(self, minute_token: str) -> None:
        item = self._items.get(minute_token)
        if item is None:
            return
        item.percent = 100
        item.stage = "下载完成"
        item.status = "completed"
        item.updated_at = datetime.now(timezone.utc).isoformat()

    def fail(self, minute_token: str, message: str) -> None:
        item = self._items.get(minute_token)
        if item is None:
            item = DownloadProgress(minute_token=minute_token)
            self._items[minute_token] = item
        item.status = "failed"
        item.stage = "下载失败"
        item.error_message = message
        item.updated_at = datetime.now(timezone.utc).isoformat()

    def get(self, minute_token: str) -> DownloadProgress | None:
        return self._items.get(minute_token)

    def clear(self, minute_token: str) -> None:
        self._items.pop(minute_token, None)


download_progress_store = DownloadProgressStore()
