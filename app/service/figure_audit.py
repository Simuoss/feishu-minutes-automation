"""配图清单与脱敏审计的序列化（落盘到 agent/）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.service.figure_redaction import FigureAuditItem, SensitiveRegionEntity
from app.service.summary_illustration import PreparedFigure

logger = logging.getLogger(__name__)


def figures_to_manifest(figures: list[PreparedFigure]) -> dict[str, Any]:
    return {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "figures": [
            {
                "figure_id": fig.figure_id,
                "relative_path": fig.relative_path,
                "timestamp": fig.timestamp,
                "frame_seconds": fig.frame_seconds,
                "expect": fig.expect,
                "purpose": fig.purpose,
            }
            for fig in figures
        ],
    }


def manifest_to_figures(
    manifest: dict[str, Any],
    assets_dir: Path,
) -> list[PreparedFigure]:
    """按清单从 assets 重建 PreparedFigure；文件缺失的条目跳过。"""
    raw = manifest.get("figures")
    if not isinstance(raw, list):
        return []
    figures: list[PreparedFigure] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        figure_id = str(item.get("figure_id") or "").strip()
        if not figure_id:
            continue
        name = f"{figure_id}.jpg"
        path = assets_dir / name
        if not path.is_file():
            logger.warning(
                "配图清单中有 %s 但 assets 里找不到文件，怀疑已被脱敏放弃或清理；跳过",
                figure_id,
            )
            continue
        relative = str(item.get("relative_path") or f"assets/{name}")
        figures.append(
            PreparedFigure(
                figure_id=figure_id,
                relative_path=relative,
                path=path,
                timestamp=str(item.get("timestamp") or "00:00:00"),
                frame_seconds=float(item.get("frame_seconds") or 0.0),
                expect=str(item.get("expect") or ""),
                purpose=str(item.get("purpose") or ""),
            )
        )
    return figures


def _region_dict(region: SensitiveRegionEntity) -> dict[str, Any]:
    return {
        "label": region.label,
        "x1": region.x1,
        "y1": region.y1,
        "x2": region.x2,
        "y2": region.y2,
    }


def audit_items_to_payload(
    items: list[FigureAuditItem],
    *,
    scanned: int,
    sensitive: int,
    redacted: int,
    abandoned: int,
) -> dict[str, Any]:
    return {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "scanned": scanned,
        "sensitive": sensitive,
        "redacted": redacted,
        "abandoned": abandoned,
        "figures": [
            {
                "figure_id": item.figure_id,
                "sensitive": item.sensitive,
                "status": item.status,
                "regions": [_region_dict(r) for r in item.regions],
                "attempts": [
                    {
                        "attempt": a.attempt,
                        "approved": a.approved,
                        "reason": a.reason,
                        "regions": [_region_dict(r) for r in a.regions],
                    }
                    for a in item.attempts
                ],
                "abandon_reason": item.abandon_reason,
                "original_relative": item.original_relative,
                "asset_relative": item.asset_relative,
            }
            for item in items
        ],
    }


def parse_regions_from_request(raw: list[dict[str, Any]]) -> list[SensitiveRegionEntity]:
    from app.service.figure_redaction import parse_regions_payload

    return parse_regions_payload(raw)


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(
            "读取 JSON 失败 path=%s，怀疑写入中断或文件损坏: %s",
            path,
            exc,
        )
        return None
    return data if isinstance(data, dict) else None


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
