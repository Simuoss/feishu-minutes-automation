"""运行时业务配置：表 system_configs 为源，内存缓存供同步读。

密钥与基础设施项仍在 Settings/.env；此处只覆盖可热改的业务开关与阈值。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_cache: dict[str, str] = {}


def replace_cache(mapping: dict[str, str]) -> None:
    _cache.clear()
    _cache.update(mapping)


def get_raw(key: str, default: str = "") -> str:
    if key in _cache:
        return _cache[key]
    return default


def get_str(key: str, default: str = "") -> str:
    return get_raw(key, default)


def get_int(key: str, default: int = 0) -> int:
    raw = get_raw(key, str(default)).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.error(
            "系统配置 %s 无法解析为整数，将回落默认值 %s；怀疑超管写入了非数字。raw=%s",
            key,
            default,
            raw,
        )
        return default


def get_float(key: str, default: float = 0.0) -> float:
    raw = get_raw(key, str(default)).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.error(
            "系统配置 %s 无法解析为浮点数，将回落默认值 %s；怀疑超管写入了非法格式。raw=%s",
            key,
            default,
            raw,
        )
        return default


def get_bool(key: str, default: bool = False) -> bool:
    raw = get_raw(key, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    logger.error(
        "系统配置 %s 无法解析为布尔，将回落默认值 %s；怀疑超管写入了非法格式。raw=%s",
        key,
        default,
        raw,
    )
    return default


def frame_burst_offset_list() -> list[float]:
    raw = get_str("FRAME_BURST_OFFSETS", "-2,1,4,8")
    offsets: list[float] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            offsets.append(float(item))
        except ValueError:
            continue
    return offsets or [0.0]


def snapshot() -> dict[str, str]:
    return dict(_cache)


def _as_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
