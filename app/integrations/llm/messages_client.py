import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# 429 是并发排队被拒，5xx 多为服务端瞬时故障，两类都值得退避重试
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}

STOP_REASON_TRUNCATED = "max_tokens"

# Anthropic Messages 必传 max_tokens；Step Plan 文档给出的上限是 250K
PLATFORM_MAX_OUTPUT_TOKENS = 250_000


class LlmRequestError(RuntimeError):
    """调用大模型失败，且已用尽重试次数。"""

    def __init__(self, message: str, *, status_code: int | None = None, attempts: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


class _RetryableError(Exception):
    """单次请求失败，但值得退避后重试。"""

    def __init__(self, status_code: int | None, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class LlmCompletion:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    attempts: int
    elapsed_seconds: float

    @property
    def truncated(self) -> bool:
        return self.stop_reason == STOP_REASON_TRUNCATED


@dataclass
class LlmImage:
    """随请求一起发给模型的图片。"""

    data: bytes
    media_type: str = "image/jpeg"

    def to_block(self) -> dict[str, Any]:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": base64.b64encode(self.data).decode(),
            },
        }


class LlmClient(Protocol):
    """业务侧依赖的最小模型接口，便于替换实现或在测试中注入假模型。"""

    async def complete(
        self,
        system_prompt: str,
        user_content: str,
        *,
        images: list[LlmImage] | None = None,
        max_tokens: int | None = None,
        max_attempts: int | None = None,
        on_attempt: Callable[[int, int], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_restart: Callable[[], None] | None = None,
    ) -> LlmCompletion: ...


class AnthropicMessagesClient:
    """Anthropic Messages API 兼容客户端（当前对接阶跃星辰）。

    统一走流式：一方面长文本生成可以边生成边展示，另一方面服务端在长任务上
    5xx 概率明显偏高，流式能更早发现连接中断。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self._base_url = (base_url or settings.llm_base_url).rstrip("/")
        self._api_key = api_key or settings.llm_api_key
        self._model = model or settings.llm_model

    async def complete(
        self,
        system_prompt: str,
        user_content: str,
        *,
        images: list[LlmImage] | None = None,
        max_tokens: int | None = None,
        max_attempts: int | None = None,
        on_attempt: Callable[[int, int], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_restart: Callable[[], None] | None = None,
    ) -> LlmCompletion:
        """发起一次流式补全，失败时按退避策略重试。

        images 非空时按多模态消息发送，图片排在文字之前（模型先看图再读要求）；
        on_attempt 在每次请求发出前回调 (当前次数, 总次数)；
        on_delta 收到文本增量时回调；
        on_restart 在"已经吐出部分内容后又失败"时回调，提示上层丢弃这些内容。
        """
        if not self._api_key:
            raise LlmRequestError("未配置 LLM_API_KEY，无法生成纪要")

        attempts_limit = max_attempts or settings.llm_max_attempts
        content: str | list[dict[str, Any]] = user_content
        if images:
            content = [image.to_block() for image in images]
            content.append({"type": "text", "text": user_content})

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._resolve_max_tokens(max_tokens),
            "system": system_prompt,
            "messages": [{"role": "user", "content": content}],
            "stream": True,
        }
        # 阶跃 Step Plan / step-explore 要求 Authorization: Bearer；
        # 同时带上 x-api-key，兼容标准 Anthropic Messages 客户端习惯。
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        started = time.perf_counter()
        last_status: int | None = None
        last_detail = ""

        for attempt in range(1, attempts_limit + 1):
            if on_attempt is not None:
                on_attempt(attempt, attempts_limit)

            received_any = False

            def track_delta(chunk: str) -> None:
                nonlocal received_any
                received_any = True
                if on_delta is not None:
                    on_delta(chunk)

            try:
                return await self._stream_once(payload, headers, track_delta, attempt, started)
            except _RetryableError as exc:
                last_status, last_detail = exc.status_code, exc.detail
                logger.warning(
                    "纪要生成暂时失败 status=%s（第 %d/%d 次），怀疑服务端瞬时故障或并发超限。详情: %s",
                    exc.status_code,
                    attempt,
                    attempts_limit,
                    exc.detail,
                )
            except httpx.TimeoutException as exc:
                last_status, last_detail = None, f"请求超时: {exc}"
                logger.warning(
                    "纪要生成流中断超时（第 %d/%d 次），怀疑模型侧长时间无输出，读超时阈值 %.0fs",
                    attempt,
                    attempts_limit,
                    settings.llm_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                last_status, last_detail = None, f"连接错误: {exc}"
                logger.warning(
                    "纪要生成连接异常（第 %d/%d 次），怀疑网络抖动或服务端提前断流: %s",
                    attempt,
                    attempts_limit,
                    exc,
                )

            # 已经推给前端的增量在重试后不再有效，必须让上层清空重来
            if received_any and on_restart is not None:
                on_restart()

            if attempt < attempts_limit:
                backoff = min(20 * attempt, 120)
                logger.info(
                    "纪要生成将在 %ds 后重试（第 %d/%d 次）", backoff, attempt + 1, attempts_limit
                )
                await asyncio.sleep(backoff)

        elapsed = time.perf_counter() - started
        logger.error(
            "纪要生成重试 %d 次后仍失败，累计耗时 %.0fs，怀疑模型服务处于故障期。最后一次: status=%s %s",
            attempts_limit,
            elapsed,
            last_status,
            last_detail,
        )
        raise LlmRequestError(
            f"模型服务连续 {attempts_limit} 次不可用，最后一次 status={last_status}",
            status_code=last_status,
            attempts=attempts_limit,
        )

    @staticmethod
    def _resolve_max_tokens(max_tokens: int | None) -> int:
        """解析输出上限：显式值优先；配置为 0/负数视为不限制，改用平台上限。"""
        value = settings.llm_max_tokens if max_tokens is None else max_tokens
        if value <= 0:
            return PLATFORM_MAX_OUTPUT_TOKENS
        return value

    async def _stream_once(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_delta: Callable[[str], None],
        attempt: int,
        started: float,
    ) -> LlmCompletion:
        text_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        stop_reason: str | None = None

        timeout = httpx.Timeout(
            connect=30.0,
            read=settings.llm_timeout_seconds,
            write=60.0,
            pool=30.0,
        )

        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream(
                "POST",
                f"{self._base_url}/v1/messages",
                headers=headers,
                json=payload,
            ) as response,
        ):
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", errors="replace")[:500]
                if response.status_code in RETRYABLE_STATUS:
                    raise _RetryableError(response.status_code, body)
                logger.error(
                    "纪要生成被模型服务拒绝 status=%s，非瞬时故障不再重试，"
                    "怀疑密钥失效、模型名错误或请求超出配额。响应: %s",
                    response.status_code,
                    body,
                )
                raise LlmRequestError(
                    f"模型服务返回 {response.status_code}: {body}",
                    status_code=response.status_code,
                    attempts=attempt,
                )

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue

                try:
                    event: dict[str, Any] = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(
                        "模型流式响应出现无法解析的数据行，已跳过，怀疑上游分片被截断。原始内容: %s",
                        raw[:200],
                    )
                    continue

                event_type = event.get("type")
                if event_type == "content_block_delta":
                    chunk = (event.get("delta") or {}).get("text") or ""
                    if chunk:
                        text_parts.append(chunk)
                        on_delta(chunk)
                elif event_type == "message_start":
                    usage = (event.get("message") or {}).get("usage") or {}
                    input_tokens = int(usage.get("input_tokens", 0))
                elif event_type == "message_delta":
                    stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
                    output_tokens = int(
                        (event.get("usage") or {}).get("output_tokens", output_tokens)
                    )
                elif event_type == "error":
                    raise _RetryableError(None, str(event.get("error"))[:500])

        text = "".join(text_parts)
        if not text.strip():
            raise _RetryableError(None, "模型流式返回结束但正文为空")

        return LlmCompletion(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=stop_reason,
            attempts=attempt,
            elapsed_seconds=time.perf_counter() - started,
        )
