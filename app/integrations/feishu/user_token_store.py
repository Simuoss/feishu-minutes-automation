import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TOKEN_FILE = Path("./data/feishu_user_token.json")


class FeishuUserTokenStore:
    def load(self) -> dict[str, Any] | None:
        if not TOKEN_FILE.is_file():
            return None
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            logger.error(
                "用户 Token 文件 JSON 损坏，怀疑手工编辑导致，需重新登录飞书授权"
            )
            return None

    def save(self, data: dict[str, Any]) -> None:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self) -> None:
        if TOKEN_FILE.is_file():
            TOKEN_FILE.unlink()

    def is_authorized(self) -> bool:
        data = self.load()
        if not data:
            return False
        expires_at = data.get("expires_at")
        if expires_at and datetime.now(timezone.utc).timestamp() < float(expires_at) - 60:
            return bool(data.get("access_token"))
        return bool(data.get("refresh_token") or data.get("access_token"))
