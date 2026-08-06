from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.resource_key import resource_key


@dataclass
class DownloadProgress:
    minute_token: str
    owner_user_id: int
    percent: int = 0
    stage: str = "等待中"
    status: str = "pending"
    error_message: str | None = None
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class DownloadProgressStore:
    """按 (owner, minute_token) 隔离的下载进度（进程内有效）。"""

    def __init__(self) -> None:
        self._items: dict[str, DownloadProgress] = {}

    def start(self, minute_token: str, *, owner_user_id: int) -> None:
        key = resource_key(owner_user_id, minute_token)
        self._items[key] = DownloadProgress(
            minute_token=minute_token,
            owner_user_id=owner_user_id,
            percent=0,
            stage="准备下载",
            status="downloading",
        )

    def update(
        self,
        minute_token: str,
        *,
        owner_user_id: int,
        percent: int,
        stage: str,
    ) -> None:
        key = resource_key(owner_user_id, minute_token)
        item = self._items.get(key)
        if item is None:
            item = DownloadProgress(
                minute_token=minute_token,
                owner_user_id=owner_user_id,
                status="downloading",
            )
            self._items[key] = item
        item.percent = max(0, min(100, percent))
        item.stage = stage
        item.status = "downloading"
        item.updated_at = datetime.now(timezone.utc).isoformat()

    def complete(self, minute_token: str, *, owner_user_id: int) -> None:
        key = resource_key(owner_user_id, minute_token)
        item = self._items.get(key)
        if item is None:
            # 跳过重复下载等路径可能未先 start，仍需让轮询看到终态
            item = DownloadProgress(
                minute_token=minute_token, owner_user_id=owner_user_id
            )
            self._items[key] = item
        item.percent = 100
        item.stage = "下载完成"
        item.status = "completed"
        item.updated_at = datetime.now(timezone.utc).isoformat()

    def fail(self, minute_token: str, message: str, *, owner_user_id: int) -> None:
        key = resource_key(owner_user_id, minute_token)
        item = self._items.get(key)
        if item is None:
            item = DownloadProgress(
                minute_token=minute_token, owner_user_id=owner_user_id
            )
            self._items[key] = item
        item.status = "failed"
        item.stage = "下载失败"
        item.error_message = message
        item.updated_at = datetime.now(timezone.utc).isoformat()

    def get(
        self, minute_token: str, *, owner_user_id: int
    ) -> DownloadProgress | None:
        return self._items.get(resource_key(owner_user_id, minute_token))

    def clear(self, minute_token: str, *, owner_user_id: int) -> None:
        self._items.pop(resource_key(owner_user_id, minute_token), None)


download_progress_store = DownloadProgressStore()
