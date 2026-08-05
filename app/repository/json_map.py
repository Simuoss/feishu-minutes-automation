"""Repository 层 JSON 列映射工具；Service/Router 不应直接 loads/dumps。"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def dump_json(value: Any, *, default: str = "null") -> str:
    if value is None:
        return default
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        logger.error(
            "Entity 序列化 JSON 失败，怀疑含不可序列化对象；将写入空值以免污染库。type=%s",
            type(value).__name__,
        )
        return default


def load_json(raw: str | None, *, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error(
            "ORM JSON 列解析失败，怀疑历史脏数据或截断写入；回落默认值以免拖垮读取。raw=%s",
            (raw[:200] + "…") if len(raw) > 200 else raw,
        )
        return default


def load_json_list(raw: str | None) -> list[Any]:
    data = load_json(raw, default=[])
    return data if isinstance(data, list) else []


def load_json_dict(raw: str | None) -> dict[str, Any]:
    data = load_json(raw, default={})
    return data if isinstance(data, dict) else {}
