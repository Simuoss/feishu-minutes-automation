"""系统配置：种子写入、缓存刷新、超管读写。"""

from __future__ import annotations

import logging

from app.core import runtime_config
from app.core.config import settings
from app.data_model.entity.system_config import (
    SystemConfigCreateEntity,
    SystemConfigEntity,
    SystemConfigUpdateEntity,
)
from app.repository.uow import UnitOfWork

logger = logging.getLogger(__name__)


def _bool_str(value: bool) -> str:
    return "true" if value else "false"


def default_catalog() -> list[SystemConfigCreateEntity]:
    """业务可选项目录；首次启动按 Settings 种子，之后以库为准。"""
    return [
        SystemConfigCreateEntity(
            key="LLM_MAX_TOKENS",
            description="大模型单次 max_tokens（0 表示不截断）",
            value=str(settings.llm_max_tokens),
            remark="按平台上限发送时保持 0",
        ),
        SystemConfigCreateEntity(
            key="LLM_TIMEOUT_SECONDS",
            description="大模型请求超时（秒）",
            value=str(settings.llm_timeout_seconds),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="LLM_MAX_ATTEMPTS",
            description="大模型失败重试次数",
            value=str(settings.llm_max_attempts),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="LLM_CONCURRENCY",
            description="全局大模型调用并发数（纪要/脱敏/挑图/答疑等共用）",
            value=str(settings.llm_concurrency),
            remark="仅限制上游 HTTP 调用数；纪要作业本身不再另设并发池。过大易 429",
        ),
        SystemConfigCreateEntity(
            key="DOWNLOAD_CONCURRENCY",
            description="妙记下载队列并发数",
            value=str(settings.download_concurrency),
            remark="修改后建议重启后端使池容量生效",
        ),
        SystemConfigCreateEntity(
            key="SUMMARY_AUTO_GENERATE",
            description="下载完成后自动生成纪要",
            value=_bool_str(settings.summary_auto_generate),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="MINUTE_READY_MAX_ATTEMPTS",
            description="妙记未就绪时最大重试次数",
            value=str(settings.minute_ready_max_attempts),
            remark="事件早于转写就绪（2091003）时使用",
        ),
        SystemConfigCreateEntity(
            key="MINUTE_READY_RETRY_SECONDS",
            description="妙记未就绪重试间隔（秒）",
            value=str(settings.minute_ready_retry_seconds),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="SUMMARY_ILLUSTRATE",
            description="有视频会议走配图纪要流水线",
            value=_bool_str(settings.summary_illustrate),
            remark="需要本机 ffmpeg/ffprobe",
        ),
        SystemConfigCreateEntity(
            key="SUMMARY_FIGURES_FIRST_HOUR",
            description="配图上限：首小时张数",
            value=str(settings.summary_figures_first_hour),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="SUMMARY_FIGURES_PER_EXTRA_HALF_HOUR",
            description="配图上限：每多半小时追加张数",
            value=str(settings.summary_figures_per_extra_half_hour),
            remark="不足半小时按半小时计",
        ),
        SystemConfigCreateEntity(
            key="SUMMARY_FIGURE_BATCH_SIZE",
            description="配图筛选批大小",
            value=str(settings.summary_figure_batch_size),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="SUMMARY_REDACT",
            description="成文前敏感信息扫描与马赛克",
            value=_bool_str(settings.summary_redact),
            remark="关闭则直接把候选图交给成文模型",
        ),
        SystemConfigCreateEntity(
            key="SUMMARY_REDACT_MAX_ATTEMPTS",
            description="脱敏马赛克最大复核次数",
            value=str(settings.summary_redact_max_attempts),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="SUMMARY_REDACT_MOSAIC_BLOCK",
            description="马赛克像素块边长",
            value=str(settings.summary_redact_mosaic_block),
            remark="越大越糊；区域会再外扩约 2%",
        ),
        SystemConfigCreateEntity(
            key="FRAME_MAX_WIDTH",
            description="抽帧最大宽度（像素）",
            value=str(settings.frame_max_width),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="FRAME_JPEG_QUALITY",
            description="抽帧 JPEG 质量（ffmpeg -q:v，2 最好 31 最差）",
            value=str(settings.frame_jpeg_quality),
            remark="讲课录屏多是文字，不宜压太狠",
        ),
        SystemConfigCreateEntity(
            key="FRAME_BURST_OFFSETS",
            description="候选时间点簇偏移（秒，逗号分隔）",
            value=settings.frame_burst_offsets,
            remark="讲话往往滞后于画面",
        ),
        SystemConfigCreateEntity(
            key="FRAME_REGRAB_LIMIT",
            description="抽帧重抓上限",
            value=str(settings.frame_regrab_limit),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="STEP_ASR_ENABLED",
            description="转写被截断时改走自建语音识别",
            value=_bool_str(settings.step_asr_enabled),
            remark="关闭后这类会议只能用飞书那几分钟的转写",
        ),
        SystemConfigCreateEntity(
            key="TRANSCRIPT_COVERAGE_THRESHOLD",
            description="转写覆盖率阈值（末条时间戳 / 录音时长）",
            value=str(settings.transcript_coverage_threshold),
            remark="低于该比例判定为免费版截断",
        ),
        SystemConfigCreateEntity(
            key="ASR_POLL_INTERVAL_SECONDS",
            description="转写任务轮询间隔（秒）",
            value=str(settings.asr_poll_interval_seconds),
            remark="官方建议 1~3 秒",
        ),
        SystemConfigCreateEntity(
            key="ASR_MAX_WAIT_SECONDS",
            description="等待转写结果的上限（秒）",
            value=str(settings.asr_max_wait_seconds),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="ASR_MAX_ATTEMPTS",
            description="转写接口失败重试次数",
            value=str(settings.asr_max_attempts),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="ASR_CHUNK_MINUTES",
            description="单次送识别的音频长度（分钟）",
            value=str(settings.asr_chunk_minutes),
            remark="调大容易被识别服务整段拒收",
        ),
        SystemConfigCreateEntity(
            key="ASR_CHUNK_CONCURRENCY",
            description="同时送识别的分段数",
            value=str(settings.asr_chunk_concurrency),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="ASR_BOUNDARY_SEARCH_SECONDS",
            description="分段边界向前后找静音的范围（秒）",
            value=str(settings.asr_boundary_search_seconds),
            remark="把刀口挪到没人说话处，避免劈断句子；填 0 则按整数倍硬切",
        ),
        SystemConfigCreateEntity(
            key="VOICEPRINT_HARVEST_ENABLED",
            description="用飞书转写里的真名自动提炼声纹",
            value=_bool_str(settings.voiceprint_harvest_enabled),
            remark="结果只进待确认队列，不会直接改人物库",
        ),
        SystemConfigCreateEntity(
            key="SPEAKER_MATCH_THRESHOLD",
            description="声纹匹配阈值（余弦相似度）",
            value=str(settings.speaker_match_threshold),
            remark="调高更保守，宁可判成新人；调低容易把两个人并成一个",
        ),
        SystemConfigCreateEntity(
            key="SPEAKER_SAMPLE_COUNT",
            description="每位说话人取几段样本算声纹",
            value=str(settings.speaker_sample_count),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="SPEAKER_SAMPLE_SECONDS",
            description="单段声纹样本时长（秒）",
            value=str(settings.speaker_sample_seconds),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="SPEAKER_MIN_SEGMENT_SECONDS",
            description="可用于取样的最短发言时长（秒）",
            value=str(settings.speaker_min_segment_seconds),
            remark="太短的发言算出来的声纹不稳",
        ),
        SystemConfigCreateEntity(
            key="SPEAKER_VERIFY_ENABLED",
            description="逐句复核说话人归属",
            value=_bool_str(settings.speaker_verify_enabled),
            remark="关掉则整组沿用云端分离结果，串音的句子不会被纠正",
        ),
        SystemConfigCreateEntity(
            key="SPEAKER_VERIFY_MIN_SECONDS",
            description="参与逐句复核的最短句子时长（秒）",
            value=str(settings.speaker_verify_min_seconds),
            remark="太短的句子声纹不稳，复核了反而添乱",
        ),
        SystemConfigCreateEntity(
            key="SPEAKER_VERIFY_MARGIN",
            description="改判所需的相似度优势",
            value=str(settings.speaker_verify_margin),
            remark="比当前归属像这么多才改判；调小改判更激进",
        ),
        SystemConfigCreateEntity(
            key="SPEAKER_VERIFY_CONCURRENCY",
            description="逐句复核的并发数",
            value=str(settings.speaker_verify_concurrency),
            remark="每句要切片加一次推理，吃 CPU",
        ),
        SystemConfigCreateEntity(
            key="R2_SIGNED_URL_TTL_SECONDS",
            description="R2 签名 URL 有效期（秒）",
            value=str(settings.r2_signed_url_ttl_seconds),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="R2_VIDEO_CRF",
            description="分享页压缩视频 CRF",
            value=str(settings.r2_video_crf),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="R2_VIDEO_MAX_HEIGHT",
            description="分享页压缩视频最大高度",
            value=str(settings.r2_video_max_height),
            remark="",
        ),
        SystemConfigCreateEntity(
            key="R2_VIDEO_PRESET",
            description="分享页压缩视频 preset",
            value=settings.r2_video_preset,
            remark="",
        ),
        SystemConfigCreateEntity(
            key="R2_VIDEO_COMPRESS_TIMEOUT_SECONDS",
            description="分享页视频压缩超时（秒）",
            value=str(settings.r2_video_compress_timeout_seconds),
            remark="长课可能很久",
        ),
        SystemConfigCreateEntity(
            key="EXPORT_WATERMARK_TEXT",
            description="导出 PDF/Word 水印文案（留空则不加水印）",
            value=str(settings.export_watermark_text or ""),
            remark="仅作用于纪要 PDF/DOCX；转写 md/txt 不加",
        ),
    ]


class SystemConfigService:
    async def ensure_seeded_and_load(self) -> None:
        catalog = default_catalog()
        async with UnitOfWork() as uow:
            assert uow.system_configs is not None
            await uow.system_configs.ensure_defaults(catalog)
            # LLM_CONCURRENCY：语义改为全局调用池；仍为历史默认 4 时升到 20，并刷新说明
            llm_seed = next((c for c in catalog if c.key == "LLM_CONCURRENCY"), None)
            existing = await uow.system_configs.get("LLM_CONCURRENCY")
            if llm_seed is not None and existing is not None:
                bump = existing.value.strip() == "4"
                await uow.system_configs.update(
                    SystemConfigUpdateEntity(
                        key="LLM_CONCURRENCY",
                        description=llm_seed.description,
                        remark=llm_seed.remark,
                        value=str(settings.llm_concurrency) if bump else None,
                    )
                )
                if bump:
                    logger.info(
                        "已将 LLM_CONCURRENCY 从历史默认 4 提升为 %s（全局大模型调用池）",
                        settings.llm_concurrency,
                    )
            await uow.commit()
            items = await uow.system_configs.list_all()
        runtime_config.replace_cache({row.key: row.value for row in items})
        self._sync_pools()
        logger.info(
            "系统配置已加载 %s 项；业务开关将以配置表为准，.env 仅作首次种子",
            len(items),
        )

    @staticmethod
    def _sync_pools() -> None:
        from app.service.download_pool import download_pool
        from app.service.llm_call_pool import llm_call_pool
        from app.service.summary_generation_pool import summary_generation_pool

        download_pool.reconfigure_from_runtime()
        llm_call_pool.reconfigure_from_runtime()
        summary_generation_pool.reconfigure_from_runtime()

    async def reload_cache(self) -> None:
        async with UnitOfWork() as uow:
            assert uow.system_configs is not None
            items = await uow.system_configs.list_all()
        runtime_config.replace_cache({row.key: row.value for row in items})
        self._sync_pools()

    async def list_all(self) -> list[SystemConfigEntity]:
        async with UnitOfWork() as uow:
            assert uow.system_configs is not None
            return await uow.system_configs.list_all()

    async def update(
        self,
        key: str,
        *,
        description: str | None = None,
        value: str | None = None,
        remark: str | None = None,
    ) -> SystemConfigEntity | None:
        key = (key or "").strip().upper()
        if not key:
            return None
        async with UnitOfWork() as uow:
            assert uow.system_configs is not None
            updated = await uow.system_configs.update(
                SystemConfigUpdateEntity(
                    key=key,
                    description=description,
                    value=value,
                    remark=remark,
                )
            )
            if updated is None:
                return None
            await uow.commit()
        await self.reload_cache()
        return updated

    async def create(self, entity: SystemConfigCreateEntity) -> SystemConfigEntity:
        entity.key = (entity.key or "").strip().upper()
        if not entity.key:
            raise ValueError("配置 key 不能为空")
        async with UnitOfWork() as uow:
            assert uow.system_configs is not None
            existing = await uow.system_configs.get(entity.key)
            if existing is not None:
                raise ValueError(f"配置 key 已存在：{entity.key}")
            created = await uow.system_configs.upsert(entity)
            await uow.commit()
        await self.reload_cache()
        return created


system_config_service = SystemConfigService()
