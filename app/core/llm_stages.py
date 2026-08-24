"""启动时把七组模型出处打出来，并挑出配了一半的组。

七组是：兜底默认，加上纪要成文、挑图、配图脱敏、场景判定、划词答疑、检索助手。
每组的地址、密钥、模型名逐项回落到兜底那组，所以只填 MODEL 是「换模型」，
三样都填是「换供应商」。只填了地址不填密钥这种半拉配置在运行时表现为 401，
不如启动就喊出来。
"""

from __future__ import annotations

import logging

from app.core.config import LlmEndpoint, settings

logger = logging.getLogger(__name__)


def _mask(api_key: str) -> str:
    key = (api_key or "").strip()
    if not key:
        return "（空）"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def _host(base_url: str) -> str:
    return base_url.split("//", 1)[-1] or "（空）"


def half_configured(endpoint: LlmEndpoint) -> str | None:
    """返回一句人话说明这组配得不对，配得没问题就返回 None。

    三样里只填 MODEL 是正常用法（同一家换模型），所以不查。剩下的组合都是
    把两家的东西拼在一起，运行时只表现为 401 或者「模型不存在」，不好查。
    """
    if "base_url" in endpoint.overridden and "api_key" not in endpoint.overridden:
        return "换了地址但没换密钥，会拿兜底那家的密钥去打这个地址"
    if "api_key" in endpoint.overridden and "base_url" not in endpoint.overridden:
        return "换了密钥但没换地址，会拿这个密钥去打兜底那家的地址"
    if "base_url" in endpoint.overridden and "model" not in endpoint.overridden:
        return (
            f"换了地址但没填模型名，会拿兜底那家的 {endpoint.model} 去请求新地址，"
            "新供应商大概不认这个名字"
        )
    return None


def suspicious_base_url(endpoint: LlmEndpoint) -> str | None:
    """地址后面会自动接 /v1/messages，所以自带 /v1 的地址必然拼错。

    换供应商时照抄文档里的 endpoint 很容易把 /v1 一起抄进来，拼出来是
    /v1/v1/messages，服务端回一个 404 «Invalid URL»，不看响应体看不出是路径问题。
    """
    lowered = endpoint.base_url.rstrip("/").lower()
    if lowered.endswith("/v1/messages"):
        return "地址里不要带 /v1/messages，客户端会自己接"
    if lowered.endswith("/v1"):
        return "地址末尾的 /v1 去掉，客户端会自己接 /v1/messages，否则拼成 /v1/v1/messages"
    return None


def log_llm_wiring() -> None:
    endpoints = settings.llm_endpoints()
    for endpoint in endpoints:
        logger.info(
            "模型分工 %s：%s @ %s（密钥 %s）%s",
            endpoint.stage,
            endpoint.model,
            _host(endpoint.base_url),
            _mask(endpoint.api_key),
            "" if endpoint.overridden else "← 回落兜底",
        )
    for endpoint in endpoints:
        warning = half_configured(endpoint)
        if warning is not None:
            logger.warning("%s 这组模型配置只填了一半：%s", endpoint.stage, warning)
        path_warning = suspicious_base_url(endpoint)
        if path_warning is not None:
            logger.warning(
                "%s 的地址 %s 看着不对：%s",
                endpoint.stage,
                endpoint.base_url,
                path_warning,
            )
    if not settings.llm_api_key:
        logger.warning("LLM_API_KEY 为空；没自带密钥的环节调模型会直接失败")
