import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.core.config import settings
from app.integrations.llm.messages_client import (
    AnthropicMessagesClient,
    LlmClient,
    LlmImage,
    LlmRequestError,
)
from app.integrations.video.ffmpeg_client import (
    FfmpegClient,
    FfmpegError,
    ffmpeg_client,
)
from app.service.figure_audit import (
    audit_items_to_payload,
    figures_to_manifest,
    manifest_to_figures,
)
from app.service.figure_redaction import FigureRedactionService, RedactionOutcome
from app.service.meeting_storage_service import MeetingStorageService
from app.service.r2_media_service import r2_media_service
from app.service.summary_event_broker import SummaryStatus, summary_broker
from app.service.summary_generation_pool import (
    SummaryGenerationPool,
    summary_generation_pool,
)
from app.service.summary_illustration import (
    PreparedFigure,
    SummaryIllustrationService,
    figure_limit_for_duration,
)
from app.service.transcript_anchor import (
    align_summary_anchors,
    parse_transcript_segments,
)

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "lecture_summary.md"

SummaryRunMode = Literal["FULL", "REDACT", "WRITE", "R2_SYNC"]
VALID_MODES = {"FULL", "REDACT", "WRITE", "R2_SYNC"}


@dataclass
class SummaryResult:
    minute_token: str
    status: str
    char_count: int = 0
    anchor_total: int = 0
    anchor_aligned: int = 0
    anchor_suspicious: int = 0
    figure_planned: int = 0
    figure_used: int = 0
    truncated: bool = False
    elapsed_seconds: float = 0.0
    error_message: str | None = None
    mode: str = "FULL"


class SummaryGenerationService:
    """基于课堂转写生成 Markdown 纪要。"""

    def __init__(
        self,
        storage: MeetingStorageService | None = None,
        llm: LlmClient | None = None,
        pool: SummaryGenerationPool | None = None,
        ffmpeg: FfmpegClient | None = None,
    ) -> None:
        self._storage = storage or MeetingStorageService()
        self._llm = llm or AnthropicMessagesClient()
        self._pool = pool or summary_generation_pool
        self._illustration = SummaryIllustrationService(self._llm, ffmpeg or ffmpeg_client)
        self._redaction = FigureRedactionService(self._llm)

    def _load_prompt(self) -> str:
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        if not prompt.strip():
            raise RuntimeError(f"纪要提示词文件为空，无法生成: {PROMPT_PATH}")
        return prompt

    async def generate(
        self,
        minute_token: str,
        *,
        force: bool = False,
        mode: str = "FULL",
    ) -> SummaryResult:
        run_mode = (mode or "FULL").strip().upper()
        if run_mode not in VALID_MODES:
            message = f"不支持的生成模式: {mode}"
            summary_broker.fail(minute_token, message)
            return SummaryResult(
                minute_token=minute_token,
                status=SummaryStatus.FAILED.value,
                error_message=message,
                mode=run_mode,
            )

        if run_mode == "R2_SYNC":
            return await self._run_r2_sync(minute_token)

        if self._pool.is_active(minute_token):
            logger.info("纪要生成任务已在进行中，忽略重复提交 token=%s", minute_token)
            self._pool.sync_broker_status(minute_token)
            return SummaryResult(
                minute_token=minute_token,
                status=SummaryStatus.GENERATING.value,
                mode=run_mode,
            )

        # FULL/WRITE 在无 force 且已有纪要时跳过；REDACT 允许在已有纪要上重跑脱敏
        if run_mode in {"FULL", "WRITE"} and not force:
            existing = self._storage.read_summary(minute_token)
            if existing is not None:
                logger.info("纪要已存在，跳过重复生成 token=%s mode=%s", minute_token, run_mode)
                summary_broker.complete(
                    minute_token, existing["content"], existing.get("meta") or {}
                )
                return SummaryResult(
                    minute_token=minute_token,
                    status=SummaryStatus.COMPLETED.value,
                    char_count=len(existing["content"]),
                    mode=run_mode,
                )

        transcript = self._storage.read_transcript(minute_token)
        if not transcript or not transcript.strip():
            message = "本地没有转写文本，请先下载该会议的转写记录"
            logger.error(
                "生成纪要失败 token=%s：本地转写缺失，怀疑该会议尚未下载或下载时转写导出失败",
                minute_token,
            )
            summary_broker.fail(minute_token, message)
            return SummaryResult(
                minute_token=minute_token,
                status=SummaryStatus.FAILED.value,
                error_message=message,
                mode=run_mode,
            )

        return await self._pool.submit(
            minute_token,
            lambda: self._generate_in_pool(minute_token, transcript, run_mode),
        )

    async def _run_r2_sync(self, minute_token: str) -> SummaryResult:
        summary_broker.update(minute_token, percent=10, stage="同步配图到 R2")
        try:
            uploaded = await r2_media_service.sync_assets(minute_token)
        except Exception as exc:  # noqa: BLE001
            message = f"同步 R2 失败: {exc}"
            logger.error("配图同步 R2 失败 token=%s：%s", minute_token, exc)
            summary_broker.fail(minute_token, message)
            return SummaryResult(
                minute_token=minute_token,
                status=SummaryStatus.FAILED.value,
                error_message=message,
                mode="R2_SYNC",
            )
        existing = self._storage.read_summary(minute_token)
        content = (existing or {}).get("content") or ""
        meta = dict((existing or {}).get("meta") or {})
        meta["r2_synced_at"] = datetime.now(timezone.utc).isoformat()
        meta["r2_uploaded"] = uploaded
        if existing is not None:
            self._storage.save_summary(minute_token, content, meta)
        summary_broker.complete(minute_token, content, meta)
        logger.info("R2 同步完成 token=%s uploaded=%d", minute_token, uploaded)
        return SummaryResult(
            minute_token=minute_token,
            status=SummaryStatus.COMPLETED.value,
            char_count=len(content),
            mode="R2_SYNC",
        )

    def _load_prepared_figures(self, minute_token: str) -> list[PreparedFigure]:
        """从 DB 清单 + assets 重建配图；无清单时回退扫描 assets 目录。"""
        assets = self._storage.get_assets_dir(minute_token)
        from app.core.async_bridge import run_async
        from app.service.metadata_db_service import read_figures_manifest

        try:
            manifest = run_async(read_figures_manifest(minute_token))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "从 DB 读取配图清单失败 token=%s，将回退扫描 assets。err=%s",
                minute_token,
                exc,
            )
            manifest = None
        if manifest is not None:
            figures = manifest_to_figures(manifest, assets)
            if figures:
                return figures
        if not assets.is_dir():
            return []
        figures = []
        for path in sorted(assets.glob("fig-*.jpg")):
            figure_id = path.stem
            figures.append(
                PreparedFigure(
                    figure_id=figure_id,
                    relative_path=f"assets/{path.name}",
                    path=path,
                    timestamp="00:00:00",
                    frame_seconds=0.0,
                    expect="",
                    purpose="",
                )
            )
        return figures

    def _persist_manifest(self, minute_token: str, figures: list[PreparedFigure]) -> None:
        payload = figures_to_manifest(figures)
        from app.core.async_bridge import run_async
        from app.service.metadata_db_service import replace_figures_from_manifest

        run_async(replace_figures_from_manifest(minute_token, payload))

    def _persist_redaction_audit(
        self, minute_token: str, outcome: RedactionOutcome
    ) -> None:
        payload = audit_items_to_payload(
            outcome.items,
            scanned=outcome.scanned,
            sensitive=outcome.sensitive,
            redacted=outcome.redacted,
            abandoned=outcome.abandoned,
        )
        from app.core.async_bridge import run_async
        from app.service.metadata_db_service import replace_redaction_from_audit

        run_async(replace_redaction_from_audit(minute_token, payload))

    async def _run_redaction(
        self, minute_token: str, figures: list[PreparedFigure]
    ) -> tuple[list[PreparedFigure], dict[str, int]]:
        empty_meta = {
            "figure_scanned": 0,
            "figure_sensitive": 0,
            "figure_redacted": 0,
            "figure_abandoned": 0,
        }
        if not figures or not settings.summary_redact:
            return figures, empty_meta

        summary_broker.update(
            minute_token,
            percent=32,
            stage=f"扫描 {len(figures)} 张截图是否含敏感信息",
        )
        redact_work = self._storage.get_frames_work_dir(minute_token) / "redact"
        originals = self._storage.get_redaction_originals_dir(minute_token)
        try:
            outcome = await self._redaction.redact_figures(
                figures,
                redact_work,
                originals,
                on_stage=lambda stage: summary_broker.update(
                    minute_token, percent=35, stage=stage
                ),
                on_attempt=lambda attempt, total: summary_broker.mark_attempt(
                    minute_token,
                    attempt,
                    total,
                    label="敏感信息扫描/打码复核",
                ),
            )
        except Exception:
            logger.exception(
                "脱敏流程异常 token=%s，隐私优先丢弃全部候选截图",
                minute_token,
            )
            for fig in figures:
                fig.path.unlink(missing_ok=True)
            self._persist_redaction_audit(
                minute_token,
                RedactionOutcome(
                    figures=[],
                    scanned=len(figures),
                    abandoned=len(figures),
                    items=[],
                ),
            )
            shutil.rmtree(self._storage.get_frames_work_dir(minute_token), ignore_errors=True)
            return [], {
                "figure_scanned": len(figures),
                "figure_sensitive": 0,
                "figure_redacted": 0,
                "figure_abandoned": len(figures),
            }

        self._persist_redaction_audit(minute_token, outcome)
        shutil.rmtree(self._storage.get_frames_work_dir(minute_token), ignore_errors=True)
        return outcome.figures, {
            "figure_scanned": outcome.scanned,
            "figure_sensitive": outcome.sensitive,
            "figure_redacted": outcome.redacted,
            "figure_abandoned": outcome.abandoned,
        }

    async def _prepare_figures(
        self,
        minute_token: str,
        transcript: str,
    ) -> list[PreparedFigure]:
        """有视频时先让模型挑出想看的时刻，再抽成截图；任何环节失败都退回纯文字纪要。"""
        if not settings.summary_illustrate:
            return []

        video_path = self._storage.find_video_path(minute_token)
        if video_path is None:
            video_path = await r2_media_service.ensure_local_video(minute_token)
        if video_path is None:
            logger.info(
                "会议 %s 无本地视频且无法从 R2 拉取，按纯文字纪要生成",
                minute_token,
            )
            return []

        summary_broker.update(minute_token, percent=6, stage="探测视频信息")
        try:
            video = await self._illustration.probe_video(video_path)
        except FfmpegError as exc:
            logger.warning(
                "视频探测失败，本次降级为纯文字纪要 token=%s：%s",
                minute_token,
                exc,
            )
            return []

        if not video.has_video_stream:
            logger.info(
                "媒体文件 %s 没有视频流（可能是纯音频容器），按纯文字纪要生成",
                video_path.name,
            )
            return []

        work_dir = self._storage.get_frames_work_dir(minute_token)
        summary_broker.update(
            minute_token,
            percent=8,
            stage=f"抽取画面样本供模型判断（视频约 {video.duration_seconds / 60:.0f} 分钟）",
        )
        sample_frames = await self._illustration.extract_plan_samples(video, work_dir)
        if not sample_frames:
            logger.info("会议 %s 未能抽出规划样本帧，跳过截图规划", minute_token)
            return []

        figure_limit = figure_limit_for_duration(video.duration_seconds)
        logger.info(
            "会议 %s 视频约 %.0f 分钟，本场配图上限 %d 张",
            minute_token,
            video.duration_seconds / 60,
            figure_limit,
        )
        summary_broker.update(
            minute_token,
            percent=12,
            stage=f"根据抽帧判断是否共享屏幕并规划截图（上限 {figure_limit} 张）",
        )
        summary_broker.clear_plan_items(minute_token)
        plan = await self._illustration.plan_screenshots(
            transcript,
            sample_frames=sample_frames,
            limit=figure_limit,
            on_attempt=lambda attempt, total: summary_broker.mark_attempt(
                minute_token,
                attempt,
                total,
                label="模型正在看抽帧样本并挑选需要配图的时刻",
            ),
            on_candidate=lambda timestamp, expect: summary_broker.add_plan_item(
                minute_token, timestamp, expect
            ),
        )
        if not plan.screen_shared:
            logger.info(
                "模型依据抽帧判定会议 %s 没有共享屏幕，按纯文字纪要生成。理由：%s",
                minute_token,
                plan.reason or "（未说明）",
            )
            return []
        if not plan.items:
            logger.info("模型未挑出需要配图的时刻 token=%s，按纯文字纪要生成", minute_token)
            return []

        summary_broker.update(
            minute_token,
            percent=25,
            stage=f"抽取 {len(plan.items)} 处画面并筛选",
        )
        self._storage.clear_assets(minute_token)
        figures = await self._illustration.prepare_figures(
            minute_token,
            video,
            plan.items,
            self._storage.ensure_assets_dir(minute_token),
            work_dir,
            limit=figure_limit,
        )
        if figures:
            self._persist_manifest(minute_token, figures)
        return figures

    async def _write_summary(
        self,
        minute_token: str,
        transcript: str,
        figures: list[PreparedFigure],
        *,
        figure_prepared: int,
        redaction_meta: dict[str, int],
        mode: str,
    ) -> SummaryResult:
        segments = parse_transcript_segments(transcript)
        system_prompt = self._load_prompt()
        user_content = f"<transcript>\n{transcript}\n</transcript>"
        images: list[LlmImage] = []
        writing_label = "模型生成中"
        if figures:
            writing_label = f"模型正在读 {len(figures)} 张截图，首个字出现前约需 1~2 分钟"
            system_prompt = f"{system_prompt}\n\n{self._illustration.load_addendum()}"
            user_content = (
                f"{self._illustration.build_manifest(figures)}\n\n{user_content}"
            )
            images = self._illustration.to_images(figures)
            summary_broker.update(minute_token, percent=40, stage=writing_label)

        try:
            completion = await self._llm.complete(
                system_prompt,
                user_content,
                images=images or None,
                on_attempt=lambda attempt, total: summary_broker.mark_attempt(
                    minute_token, attempt, total, label=writing_label
                ),
                on_delta=lambda chunk: summary_broker.append_delta(minute_token, chunk),
                on_restart=lambda: summary_broker.reset_buffer(minute_token),
            )
        except LlmRequestError as exc:
            logger.error(
                "生成纪要失败 token=%s：模型服务不可用，本次已重试 %d 次。%s",
                minute_token,
                exc.attempts,
                exc,
            )
            summary_broker.fail(minute_token, str(exc))
            return SummaryResult(
                minute_token=minute_token,
                status=SummaryStatus.FAILED.value,
                error_message=str(exc),
                mode=mode,
            )
        except OSError as exc:
            logger.exception("生成纪要失败 token=%s：读取提示词文件出错", minute_token)
            summary_broker.fail(minute_token, f"提示词加载失败: {exc}")
            return SummaryResult(
                minute_token=minute_token,
                status=SummaryStatus.FAILED.value,
                error_message=f"提示词加载失败: {exc}",
                mode=mode,
            )

        if completion.truncated:
            logger.warning(
                "纪要因模型输出上限被截断 token=%s，结尾段落可能不完整；"
                "当前 LLM_MAX_TOKENS=%s（0 表示按平台上限），怀疑转写过长或模型提前停写",
                minute_token,
                settings.llm_max_tokens,
            )

        summary_broker.update(minute_token, percent=90, stage="校正时间锚点")
        alignment = align_summary_anchors(completion.text.strip(), segments)

        used_figures = figures
        if figures:
            summary_broker.update(minute_token, percent=93, stage="清理未采用的截图")
            used_figures = self._illustration.drop_unreferenced(alignment.text, figures)
            # 成文后清单只保留实际用上的图，便于 WRITE 重跑
            self._persist_manifest(minute_token, used_figures)

        summary_broker.update(minute_token, percent=95, stage="写入本地文件")
        meta = {
            "minute_token": minute_token,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": settings.llm_model,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "attempts": completion.attempts,
            "elapsed_seconds": round(completion.elapsed_seconds, 1),
            "stop_reason": completion.stop_reason,
            "truncated": completion.truncated,
            "transcript_chars": len(transcript),
            "summary_chars": len(alignment.text),
            "anchor_total": alignment.total,
            "anchor_aligned": alignment.aligned,
            "anchor_suspicious": alignment.suspicious,
            "figure_planned": figure_prepared,
            "figure_for_writing": len(figures),
            "figure_used": len(used_figures),
            "run_mode": mode,
            **redaction_meta,
        }
        # 合并已有脱敏统计（WRITE 模式未重跑脱敏时保留上次）
        if mode == "WRITE":
            previous = self._storage.read_summary(minute_token)
            prev_meta = (previous or {}).get("meta") or {}
            for key in (
                "figure_scanned",
                "figure_sensitive",
                "figure_redacted",
                "figure_abandoned",
            ):
                if meta.get(key, 0) == 0 and prev_meta.get(key) is not None:
                    meta[key] = prev_meta.get(key)

        self._storage.save_summary(minute_token, alignment.text, meta)
        await r2_media_service.sync_assets_safe(minute_token)
        summary_broker.complete(minute_token, alignment.text, meta)

        logger.info(
            "纪要生成完成 token=%s mode=%s 用时 %.0fs / %d 次尝试 / 输出 %d 字 / "
            "锚点 %d 个（吸附 %d）/ 配图 %d 张（备选 %d）",
            minute_token,
            mode,
            completion.elapsed_seconds,
            completion.attempts,
            len(alignment.text),
            alignment.total,
            alignment.aligned,
            len(used_figures),
            len(figures),
        )
        return SummaryResult(
            minute_token=minute_token,
            status=SummaryStatus.COMPLETED.value,
            char_count=len(alignment.text),
            anchor_total=alignment.total,
            anchor_aligned=alignment.aligned,
            anchor_suspicious=len(alignment.suspicious),
            figure_planned=figure_prepared,
            figure_used=len(used_figures),
            truncated=completion.truncated,
            elapsed_seconds=completion.elapsed_seconds,
            mode=mode,
        )

    async def _generate_in_pool(
        self,
        minute_token: str,
        transcript: str,
        mode: str = "FULL",
    ) -> SummaryResult:
        from app.service.pipeline_job_service import start_job, update_job

        job_id = await start_job(
            minute_token, job_type="SUMMARY", mode=mode, stage="START"
        )
        try:
            summary_broker.update(minute_token, percent=5, stage="解析转写文本")
            await update_job(job_id, stage="PARSE_TRANSCRIPT", percent=5.0)
            segments = parse_transcript_segments(transcript)
            logger.info(
                "开始生成纪要 token=%s mode=%s 转写 %d 字 / %d 个发言段落",
                minute_token,
                mode,
                len(transcript),
                len(segments),
            )

            redaction_meta = {
                "figure_scanned": 0,
                "figure_sensitive": 0,
                "figure_redacted": 0,
                "figure_abandoned": 0,
            }
            figure_prepared = 0
            figures: list[PreparedFigure] = []

            if mode == "FULL":
                try:
                    figures = await self._prepare_figures(minute_token, transcript)
                except LlmRequestError as exc:
                    logger.warning(
                        "截图计划阶段模型不可用，本次降级为纯文字纪要 token=%s：%s",
                        minute_token,
                        exc,
                    )
                    figures = []
                except FfmpegError as exc:
                    logger.warning(
                        "抽帧失败，本次降级为纯文字纪要 token=%s：%s",
                        minute_token,
                        exc,
                    )
                    figures = []
                figure_prepared = len(figures)
                figures, redaction_meta = await self._run_redaction(minute_token, figures)
                result = await self._write_summary(
                    minute_token,
                    transcript,
                    figures,
                    figure_prepared=figure_prepared,
                    redaction_meta=redaction_meta,
                    mode=mode,
                )
                await update_job(
                    job_id,
                    status=result.status,
                    stage="DONE",
                    percent=100.0,
                    error_message=result.error_message,
                    finished=True,
                )
                return result

            if mode == "REDACT":
                figures = self._load_prepared_figures(minute_token)
                if not figures:
                    message = "没有可脱敏的配图，请先完整生成一次带图纪要"
                    summary_broker.fail(minute_token, message)
                    await update_job(
                        job_id,
                        status="FAILED",
                        stage="FAILED",
                        error_message=message,
                        finished=True,
                    )
                    return SummaryResult(
                        minute_token=minute_token,
                        status=SummaryStatus.FAILED.value,
                        error_message=message,
                        mode=mode,
                    )
                figure_prepared = len(figures)
                # 用原图目录覆盖 assets 再跑，避免对已打码图二次扫描
                originals = self._storage.get_redaction_originals_dir(minute_token)
                restored: list[PreparedFigure] = []
                for fig in figures:
                    original = originals / f"{fig.figure_id}.jpg"
                    if original.is_file():
                        shutil.copy2(original, fig.path)
                    if fig.path.is_file():
                        restored.append(fig)
                figures, redaction_meta = await self._run_redaction(minute_token, restored)
                self._persist_manifest(minute_token, figures)
                # 更新 meta 中的脱敏统计，保留正文
                existing = self._storage.read_summary(minute_token)
                content = (existing or {}).get("content") or ""
                meta = dict((existing or {}).get("meta") or {})
                meta.update(redaction_meta)
                meta["redacted_at"] = datetime.now(timezone.utc).isoformat()
                meta["run_mode"] = mode
                if existing is not None:
                    self._storage.save_summary(minute_token, content, meta)
                summary_broker.complete(minute_token, content, meta)
                logger.info(
                    "仅脱敏完成 token=%s 扫描 %d / 打码 %d / 放弃 %d",
                    minute_token,
                    redaction_meta["figure_scanned"],
                    redaction_meta["figure_redacted"],
                    redaction_meta["figure_abandoned"],
                )
                await update_job(
                    job_id, status="COMPLETED", stage="DONE", percent=100.0, finished=True
                )
                return SummaryResult(
                    minute_token=minute_token,
                    status=SummaryStatus.COMPLETED.value,
                    char_count=len(content),
                    figure_planned=figure_prepared,
                    figure_used=len(figures),
                    mode=mode,
                )

            # WRITE：跳过抽帧与脱敏，直接成文
            figures = self._load_prepared_figures(minute_token)
            figure_prepared = len(figures)
            result = await self._write_summary(
                minute_token,
                transcript,
                figures,
                figure_prepared=figure_prepared,
                redaction_meta=redaction_meta,
                mode=mode,
            )
            await update_job(
                job_id,
                status=result.status,
                stage="DONE",
                percent=100.0,
                error_message=result.error_message,
                finished=True,
            )
            return result
        except Exception as exc:
            await update_job(
                job_id,
                status="FAILED",
                stage="FAILED",
                error_message=str(exc),
                finished=True,
            )
            raise


summary_generation_service = SummaryGenerationService()
