"""脱敏审计读取与人工复核（改框放行 / 放弃）。"""

from __future__ import annotations

import logging
from typing import Any

from app.core.async_bridge import run_async
from app.core.config import settings
from app.service.figure_audit import parse_regions_from_request
from app.service.image_mosaic import mosaic_file
from app.service.meeting_storage_service import MeetingStorageService
from app.service.metadata_db_service import (
    read_redaction_audit,
    replace_redaction_from_audit,
)

logger = logging.getLogger(__name__)


class RedactionReviewService:
    def __init__(self, storage: MeetingStorageService | None = None) -> None:
        self._storage = storage or MeetingStorageService()

    def read_audit(self, minute_token: str) -> dict[str, Any] | None:
        try:
            return run_async(read_redaction_audit(minute_token))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "从 DB 读脱敏审计失败 token=%s；怀疑 schema 未就绪。err=%s",
                minute_token,
                exc,
            )
            return None

    def _upsert_figure(self, audit: dict[str, Any], figure: dict[str, Any]) -> dict[str, Any]:
        figures = list(audit.get("figures") or [])
        replaced = False
        for index, item in enumerate(figures):
            if isinstance(item, dict) and item.get("figure_id") == figure["figure_id"]:
                figures[index] = figure
                replaced = True
                break
        if not replaced:
            figures.append(figure)
        audit["figures"] = figures
        sensitive = sum(1 for f in figures if isinstance(f, dict) and f.get("sensitive"))
        redacted = sum(
            1
            for f in figures
            if isinstance(f, dict) and f.get("status") in {"REDACTED", "MANUAL_APPROVED"}
        )
        abandoned = sum(
            1 for f in figures if isinstance(f, dict) and f.get("status") == "ABANDONED"
        )
        audit["sensitive"] = sensitive
        audit["redacted"] = redacted
        audit["abandoned"] = abandoned
        audit["scanned"] = len(figures)
        return audit

    def _persist_audit(self, minute_token: str, audit: dict[str, Any]) -> None:
        run_async(replace_redaction_from_audit(minute_token, audit))

    def approve(
        self,
        minute_token: str,
        figure_id: str,
        regions_raw: list[dict[str, Any]],
    ) -> dict[str, Any]:
        regions = parse_regions_from_request(regions_raw)
        if not regions:
            raise ValueError("区域坐标无效，无法打码")

        original = self._storage.resolve_agent_relative_path(
            minute_token, f"agent/redaction/originals/{figure_id}.jpg"
        )
        if original is None:
            raise FileNotFoundError(
                f"找不到 {figure_id} 的脱敏原图，怀疑尚未跑过脱敏或原图已被清理"
            )

        assets = self._storage.ensure_assets_dir(minute_token)
        dest = assets / f"{figure_id}.jpg"
        mosaic_regions = []
        for region in regions:
            mosaic = region.to_mosaic(pad=0.02)
            if mosaic is not None:
                mosaic_regions.append(mosaic)
        if not mosaic_regions:
            raise ValueError("区域坐标无效，无法打码")
        mosaic_file(
            original,
            dest,
            mosaic_regions,
            block_size=settings.summary_redact_mosaic_block,
        )

        audit = self.read_audit(minute_token) or {
            "figures": [],
            "scanned": 0,
            "sensitive": 0,
            "redacted": 0,
            "abandoned": 0,
        }
        figure = {
            "figure_id": figure_id,
            "sensitive": True,
            "status": "MANUAL_APPROVED",
            "regions": [
                {
                    "label": r.label,
                    "x1": r.x1,
                    "y1": r.y1,
                    "x2": r.x2,
                    "y2": r.y2,
                }
                for r in regions
            ],
            "attempts": [],
            "abandon_reason": None,
            "original_relative": f"agent/redaction/originals/{figure_id}.jpg",
            "asset_relative": f"assets/{figure_id}.jpg",
        }
        audit = self._upsert_figure(audit, figure)
        self._persist_audit(minute_token, audit)
        logger.info("人工放行脱敏配图 token=%s figure=%s", minute_token, figure_id)
        return audit

    def abandon(
        self,
        minute_token: str,
        figure_id: str,
        reason: str,
    ) -> dict[str, Any]:
        asset = self._storage.resolve_asset_path(minute_token, f"{figure_id}.jpg")
        if asset is not None:
            asset.unlink(missing_ok=True)

        audit = self.read_audit(minute_token) or {
            "figures": [],
            "scanned": 0,
            "sensitive": 0,
            "redacted": 0,
            "abandoned": 0,
        }
        figure = {
            "figure_id": figure_id,
            "sensitive": True,
            "status": "ABANDONED",
            "regions": [],
            "attempts": [],
            "abandon_reason": reason or "人工放弃",
            "original_relative": f"agent/redaction/originals/{figure_id}.jpg",
            "asset_relative": None,
        }
        for item in audit.get("figures") or []:
            if isinstance(item, dict) and item.get("figure_id") == figure_id:
                figure["regions"] = item.get("regions") or []
                figure["attempts"] = item.get("attempts") or []
                break
        audit = self._upsert_figure(audit, figure)
        self._persist_audit(minute_token, audit)
        logger.info(
            "人工放弃配图 token=%s figure=%s reason=%s",
            minute_token,
            figure_id,
            reason,
        )
        return audit


redaction_review_service = RedactionReviewService()
