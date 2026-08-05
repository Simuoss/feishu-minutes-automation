"""成文前对候选截图做敏感信息扫描、马赛克打码与复核。

流程：扫描全部候选图 -> 对敏感图打码 -> 原图+打码图复核 -> 不准则重盖（最多 N 次）
-> 仍不准则丢弃该图。无敏感图则原样返回。
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings
from app.integrations.llm.messages_client import LlmClient, LlmImage, LlmRequestError
from app.service.image_mosaic import MosaicRegion, mosaic_file, normalize_region
from app.service.summary_illustration import PreparedFigure, extract_json_object

logger = logging.getLogger(__name__)

SCAN_PROMPT_PATH = Path(__file__).parent / "prompts" / "sensitive_scan.md"
VERIFY_PROMPT_PATH = Path(__file__).parent / "prompts" / "sensitive_verify.md"

SCAN_MAX_TOKENS = 8192
VERIFY_MAX_TOKENS = 2048


@dataclass
class SensitiveRegionEntity:
    label: str
    x1: float
    y1: float
    x2: float
    y2: float

    def to_mosaic(self, *, pad: float = 0.02) -> MosaicRegion | None:
        return normalize_region(
            self.x1,
            self.y1,
            self.x2,
            self.y2,
            label=self.label,
            pad=pad,
        )


@dataclass
class FigureScanItem:
    figure_id: str
    sensitive: bool
    regions: list[SensitiveRegionEntity] = field(default_factory=list)


@dataclass
class VerifyResult:
    figure_id: str
    approved: bool
    reason: str
    regions: list[SensitiveRegionEntity] = field(default_factory=list)


@dataclass
class RedactAttemptRecord:
    attempt: int
    approved: bool | None
    reason: str
    regions: list[SensitiveRegionEntity] = field(default_factory=list)


@dataclass
class FigureAuditItem:
    figure_id: str
    sensitive: bool
    status: str  # CLEAN | REDACTED | ABANDONED | MANUAL_APPROVED
    regions: list[SensitiveRegionEntity] = field(default_factory=list)
    attempts: list[RedactAttemptRecord] = field(default_factory=list)
    abandon_reason: str | None = None
    original_relative: str | None = None
    asset_relative: str | None = None


@dataclass
class RedactionOutcome:
    figures: list[PreparedFigure]
    scanned: int = 0
    sensitive: int = 0
    redacted: int = 0
    abandoned: int = 0
    items: list[FigureAuditItem] = field(default_factory=list)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_regions_payload(raw: object) -> list[SensitiveRegionEntity]:
    """供扫描/复核解析与人工审核 API 共用。"""
    return _parse_regions(raw)


def _parse_regions(raw: object) -> list[SensitiveRegionEntity]:
    if not isinstance(raw, list):
        return []
    regions: list[SensitiveRegionEntity] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        x1 = _as_float(item.get("x1"))
        y1 = _as_float(item.get("y1"))
        x2 = _as_float(item.get("x2"))
        y2 = _as_float(item.get("y2"))
        if x1 is None or y1 is None or x2 is None or y2 is None:
            continue
        label = str(item.get("label") or "").strip() or "敏感信息"
        normalized = normalize_region(x1, y1, x2, y2, label=label, pad=0.0)
        if normalized is None:
            continue
        regions.append(
            SensitiveRegionEntity(
                label=normalized.label,
                x1=normalized.x1,
                y1=normalized.y1,
                x2=normalized.x2,
                y2=normalized.y2,
            )
        )
    return regions


def parse_scan_response(
    text: str,
    expected_ids: list[str],
) -> list[FigureScanItem]:
    """解析扫描 JSON；缺项按「非敏感」填齐，避免漏图阻断整批。"""
    payload = extract_json_object(text)
    by_id: dict[str, FigureScanItem] = {}
    if payload is not None:
        figures = payload.get("figures")
        if isinstance(figures, list):
            for item in figures:
                if not isinstance(item, dict):
                    continue
                figure_id = str(item.get("figure_id") or "").strip()
                if not figure_id:
                    continue
                regions = _parse_regions(item.get("regions"))
                sensitive = bool(item.get("sensitive")) and bool(regions)
                if bool(item.get("sensitive")) and not regions:
                    logger.warning(
                        "敏感扫描把 %s 标为敏感却未给坐标，怀疑模型漏写 regions；"
                        "本张按非敏感放行，成文侧提示词仍会要求跳过可读密钥",
                        figure_id,
                    )
                by_id[figure_id] = FigureScanItem(
                    figure_id=figure_id,
                    sensitive=sensitive,
                    regions=regions,
                )

    results: list[FigureScanItem] = []
    for figure_id in expected_ids:
        item = by_id.get(figure_id)
        if item is None:
            logger.warning(
                "敏感扫描结果缺少 %s，怀疑模型漏报；按非敏感处理以免整批配图作废",
                figure_id,
            )
            results.append(FigureScanItem(figure_id=figure_id, sensitive=False))
            continue
        results.append(item)
    return results


def parse_verify_response(text: str, figure_id: str) -> VerifyResult | None:
    payload = extract_json_object(text)
    if payload is None:
        return None
    approved = bool(payload.get("approved"))
    reason = str(payload.get("reason") or "").strip()
    regions = _parse_regions(payload.get("regions"))
    raw_id = str(payload.get("figure_id") or "").strip() or figure_id
    if approved:
        return VerifyResult(
            figure_id=raw_id,
            approved=True,
            reason=reason or "通过",
            regions=[],
        )
    if not regions:
        logger.warning(
            "打码复核判定 %s 不通过却未给出修正坐标，怀疑模型只写了 reason；本次无法重盖",
            figure_id,
        )
        return VerifyResult(
            figure_id=raw_id,
            approved=False,
            reason=reason or "不通过且无修正坐标",
            regions=[],
        )
    return VerifyResult(
        figure_id=raw_id,
        approved=False,
        reason=reason or "不通过",
        regions=regions,
    )


class FigureRedactionService:
    """成文前脱敏：扫描 -> 打码 -> 复核循环。"""

    def __init__(self, llm: LlmClient) -> None:
        self._llm = llm

    def _load_scan_prompt(self) -> str:
        return SCAN_PROMPT_PATH.read_text(encoding="utf-8")

    def _load_verify_prompt(self) -> str:
        return VERIFY_PROMPT_PATH.read_text(encoding="utf-8")

    @staticmethod
    def _manifest(figures: list[PreparedFigure]) -> str:
        lines = ["<figures>"]
        for fig in figures:
            lines.append(f"- {fig.figure_id} | 路径 `{fig.relative_path}`")
        lines.append("</figures>")
        return "\n".join(lines)

    async def _scan_batch(
        self,
        figures: list[PreparedFigure],
        *,
        on_attempt: Callable[[int, int], None] | None = None,
    ) -> list[FigureScanItem]:
        images = [LlmImage(data=fig.path.read_bytes()) for fig in figures]
        user_content = (
            f"请扫描以下 {len(figures)} 张截图是否含敏感信息。"
            f"图片顺序与清单一致。\n\n{self._manifest(figures)}"
        )
        completion = await self._llm.complete(
            self._load_scan_prompt(),
            user_content,
            images=images,
            max_tokens=SCAN_MAX_TOKENS,
            on_attempt=on_attempt,
        )
        return parse_scan_response(
            completion.text,
            [fig.figure_id for fig in figures],
        )

    async def scan_figures(
        self,
        figures: list[PreparedFigure],
        *,
        on_attempt: Callable[[int, int], None] | None = None,
    ) -> dict[str, FigureScanItem]:
        """分批扫描全部候选图，返回 figure_id -> 扫描结果。"""
        if not figures:
            return {}

        batch_size = max(1, settings.summary_figure_batch_size)
        merged: dict[str, FigureScanItem] = {}
        for start in range(0, len(figures), batch_size):
            batch = figures[start : start + batch_size]
            items = await self._scan_batch(batch, on_attempt=on_attempt)
            for item in items:
                merged[item.figure_id] = item
        return merged

    async def _verify_once(
        self,
        figure: PreparedFigure,
        original_bytes: bytes,
        redacted_path: Path,
        *,
        on_attempt: Callable[[int, int], None] | None = None,
    ) -> VerifyResult | None:
        images = [
            LlmImage(data=original_bytes),
            LlmImage(data=redacted_path.read_bytes()),
        ]
        user_content = (
            f"figure_id=`{figure.figure_id}`。\n"
            "第 1 张是原图，第 2 张是打码图。请判断打码是否准确。"
        )
        completion = await self._llm.complete(
            self._load_verify_prompt(),
            user_content,
            images=images,
            max_tokens=VERIFY_MAX_TOKENS,
            on_attempt=on_attempt,
        )
        return parse_verify_response(completion.text, figure.figure_id)

    def _write_mosaic(
        self,
        original_path: Path,
        dest_path: Path,
        regions: list[SensitiveRegionEntity],
    ) -> list[MosaicRegion]:
        mosaic_regions: list[MosaicRegion] = []
        for region in regions:
            mosaic = region.to_mosaic(pad=0.02)
            if mosaic is not None:
                mosaic_regions.append(mosaic)
        if not mosaic_regions:
            raise ValueError("没有有效的打码区域")
        mosaic_file(
            original_path,
            dest_path,
            mosaic_regions,
            block_size=settings.summary_redact_mosaic_block,
        )
        return mosaic_regions

    def apply_manual_mosaic(
        self,
        original_path: Path,
        dest_path: Path,
        regions: list[SensitiveRegionEntity],
    ) -> None:
        """人工指定区域打码，覆盖 dest（通常是 assets 中的配图）。"""
        self._write_mosaic(original_path, dest_path, regions)

    async def _redact_one(
        self,
        figure: PreparedFigure,
        regions: list[SensitiveRegionEntity],
        work_dir: Path,
        original_path: Path,
        *,
        on_attempt: Callable[[int, int], None] | None = None,
    ) -> FigureAuditItem:
        """对单张敏感图打码并复核。"""
        max_attempts = max(1, settings.summary_redact_max_attempts)
        trial_path = work_dir / f"{figure.figure_id}-redacted.jpg"
        original_bytes = original_path.read_bytes()
        current_regions = list(regions)
        attempts: list[RedactAttemptRecord] = []
        original_rel = f"agent/redaction/originals/{figure.figure_id}.jpg"

        for attempt in range(1, max_attempts + 1):
            try:
                self._write_mosaic(original_path, trial_path, current_regions)
            except (OSError, ValueError) as exc:
                reason = f"打码失败: {exc}"
                logger.error(
                    "对 %s 打码失败（第 %d/%d 次），怀疑坐标无效或图片损坏，将放弃该图。"
                    "原因: %s",
                    figure.figure_id,
                    attempt,
                    max_attempts,
                    exc,
                )
                attempts.append(
                    RedactAttemptRecord(
                        attempt=attempt,
                        approved=False,
                        reason=reason,
                        regions=list(current_regions),
                    )
                )
                return FigureAuditItem(
                    figure_id=figure.figure_id,
                    sensitive=True,
                    status="ABANDONED",
                    regions=list(current_regions),
                    attempts=attempts,
                    abandon_reason=reason,
                    original_relative=original_rel,
                    asset_relative=None,
                )

            try:
                verdict = await self._verify_once(
                    figure,
                    original_bytes,
                    trial_path,
                    on_attempt=on_attempt,
                )
            except LlmRequestError as exc:
                reason = f"复核模型不可用: {exc}"
                logger.error(
                    "打码复核模型不可用 figure=%s attempt=%d/%d，隐私优先放弃该图。%s",
                    figure.figure_id,
                    attempt,
                    max_attempts,
                    exc,
                )
                attempts.append(
                    RedactAttemptRecord(
                        attempt=attempt,
                        approved=False,
                        reason=reason,
                        regions=list(current_regions),
                    )
                )
                return FigureAuditItem(
                    figure_id=figure.figure_id,
                    sensitive=True,
                    status="ABANDONED",
                    regions=list(current_regions),
                    attempts=attempts,
                    abandon_reason=reason,
                    original_relative=original_rel,
                    asset_relative=None,
                )

            if verdict is None:
                reason = "复核输出无法解析"
                logger.warning(
                    "打码复核输出无法解析 figure=%s attempt=%d/%d，怀疑模型未按 JSON 作答",
                    figure.figure_id,
                    attempt,
                    max_attempts,
                )
                attempts.append(
                    RedactAttemptRecord(
                        attempt=attempt,
                        approved=None,
                        reason=reason,
                        regions=list(current_regions),
                    )
                )
                if attempt >= max_attempts:
                    return FigureAuditItem(
                        figure_id=figure.figure_id,
                        sensitive=True,
                        status="ABANDONED",
                        regions=list(current_regions),
                        attempts=attempts,
                        abandon_reason=reason,
                        original_relative=original_rel,
                        asset_relative=None,
                    )
                continue

            attempts.append(
                RedactAttemptRecord(
                    attempt=attempt,
                    approved=verdict.approved,
                    reason=verdict.reason,
                    regions=list(verdict.regions or current_regions),
                )
            )

            if verdict.approved:
                shutil.copy2(trial_path, figure.path)
                logger.info(
                    "截图 %s 敏感区域打码通过（第 %d 次复核）",
                    figure.figure_id,
                    attempt,
                )
                return FigureAuditItem(
                    figure_id=figure.figure_id,
                    sensitive=True,
                    status="REDACTED",
                    regions=list(current_regions),
                    attempts=attempts,
                    original_relative=original_rel,
                    asset_relative=figure.relative_path,
                )

            logger.info(
                "截图 %s 打码未通过（第 %d/%d 次）：%s",
                figure.figure_id,
                attempt,
                max_attempts,
                verdict.reason,
            )
            if attempt >= max_attempts:
                return FigureAuditItem(
                    figure_id=figure.figure_id,
                    sensitive=True,
                    status="ABANDONED",
                    regions=list(current_regions),
                    attempts=attempts,
                    abandon_reason=verdict.reason or "复核连续未通过",
                    original_relative=original_rel,
                    asset_relative=None,
                )
            if not verdict.regions:
                current_regions = [
                    SensitiveRegionEntity(
                        label=r.label,
                        x1=max(0.0, r.x1 - 0.03),
                        y1=max(0.0, r.y1 - 0.03),
                        x2=min(1.0, r.x2 + 0.03),
                        y2=min(1.0, r.y2 + 0.03),
                    )
                    for r in current_regions
                ]
            else:
                current_regions = verdict.regions

        return FigureAuditItem(
            figure_id=figure.figure_id,
            sensitive=True,
            status="ABANDONED",
            regions=list(current_regions),
            attempts=attempts,
            abandon_reason="复核连续未通过",
            original_relative=original_rel,
            asset_relative=None,
        )

    async def redact_figures(
        self,
        figures: list[PreparedFigure],
        work_dir: Path,
        originals_dir: Path,
        *,
        on_stage: Callable[[str], None] | None = None,
        on_attempt: Callable[[int, int], None] | None = None,
    ) -> RedactionOutcome:
        """对候选截图执行脱敏；返回可交给成文模型的图列表，并附带审计明细。"""
        if not figures or not settings.summary_redact:
            items = [
                FigureAuditItem(
                    figure_id=fig.figure_id,
                    sensitive=False,
                    status="CLEAN",
                    asset_relative=fig.relative_path,
                )
                for fig in figures
            ]
            return RedactionOutcome(figures=list(figures), items=items)

        work_dir.mkdir(parents=True, exist_ok=True)
        originals_dir.mkdir(parents=True, exist_ok=True)
        # 打码前先留原图，供复核与人工改框；已有原图不覆盖，避免二次脱敏盖掉真原图
        for fig in figures:
            dest = originals_dir / f"{fig.figure_id}.jpg"
            if not dest.is_file() and fig.path.is_file():
                shutil.copy2(fig.path, dest)

        if on_stage:
            on_stage(f"扫描 {len(figures)} 张截图是否含敏感信息")

        try:
            scan_map = await self.scan_figures(figures, on_attempt=on_attempt)
        except LlmRequestError as exc:
            logger.error(
                "敏感扫描模型不可用，隐私优先丢弃全部候选截图，本次纪要改为纯文字。"
                "原因: %s",
                exc,
            )
            items: list[FigureAuditItem] = []
            for fig in figures:
                fig.path.unlink(missing_ok=True)
                items.append(
                    FigureAuditItem(
                        figure_id=fig.figure_id,
                        sensitive=True,
                        status="ABANDONED",
                        abandon_reason=f"敏感扫描模型不可用: {exc}",
                        original_relative=f"agent/redaction/originals/{fig.figure_id}.jpg",
                    )
                )
            return RedactionOutcome(
                figures=[],
                scanned=0,
                abandoned=len(figures),
                items=items,
            )

        sensitive_items = [item for item in scan_map.values() if item.sensitive]
        if on_stage:
            if sensitive_items:
                on_stage(f"发现 {len(sensitive_items)} 张含敏感信息，开始打码复核")
            else:
                on_stage("未发现敏感截图，进入纪要生成")

        kept: list[PreparedFigure] = []
        audit_items: list[FigureAuditItem] = []
        redacted = 0
        abandoned = 0
        for fig in figures:
            scan = scan_map.get(fig.figure_id)
            if scan is None or not scan.sensitive:
                kept.append(fig)
                audit_items.append(
                    FigureAuditItem(
                        figure_id=fig.figure_id,
                        sensitive=False,
                        status="CLEAN",
                        asset_relative=fig.relative_path,
                        original_relative=f"agent/redaction/originals/{fig.figure_id}.jpg",
                    )
                )
                continue
            if on_stage:
                on_stage(f"打码并复核 {fig.figure_id}")
            original_path = originals_dir / f"{fig.figure_id}.jpg"
            detail = await self._redact_one(
                fig,
                scan.regions,
                work_dir,
                original_path,
                on_attempt=on_attempt,
            )
            audit_items.append(detail)
            if detail.status == "REDACTED":
                kept.append(fig)
                redacted += 1
            else:
                abandoned += 1
                fig.path.unlink(missing_ok=True)
                logger.warning(
                    "截图 %s 打码复核连续失败，已丢弃该图，避免把敏感原图交给成文模型",
                    fig.figure_id,
                )

        shutil.rmtree(work_dir, ignore_errors=True)
        logger.info(
            "脱敏完成：扫描 %d 张，敏感 %d 张，打码通过 %d 张，放弃 %d 张，留给成文 %d 张",
            len(figures),
            len(sensitive_items),
            redacted,
            abandoned,
            len(kept),
        )
        return RedactionOutcome(
            figures=kept,
            scanned=len(figures),
            sensitive=len(sensitive_items),
            redacted=redacted,
            abandoned=abandoned,
            items=audit_items,
        )
