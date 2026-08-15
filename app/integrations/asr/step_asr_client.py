"""阶跃音频文件识别客户端：提交任务后轮询取结果。

走文件异步接口而不是 SSE，是因为它才支持说话人分离（enable_speaker_info），
而分离结果是本项目区分「谁说了哪句」的唯一来源。代价是音频必须给一个公网可
达的直链，这块由 R2 预签名 URL 承担。
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core import runtime_config
from app.core.config import settings

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}
SUBMIT_PATH = "/v1/audio/asr/file/submit"
QUERY_PATH = "/v1/audio/asr/file/query"

STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_FAILED = "FAILED"


class AsrRequestError(RuntimeError):
    """转写失败，且已用尽重试或被服务端明确拒绝。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class AsrUtterance:
    text: str
    start_ms: int
    end_ms: int
    speaker_id: str = ""


@dataclass
class AsrResult:
    utterances: list[AsrUtterance] = field(default_factory=list)
    text: str = ""
    duration_seconds: float = 0.0
    task_id: str = ""
    elapsed_seconds: float = 0.0

    @property
    def speaker_ids(self) -> list[str]:
        seen: list[str] = []
        for item in self.utterances:
            if item.speaker_id and item.speaker_id not in seen:
                seen.append(item.speaker_id)
        return seen


def _parse_result(payload: dict[str, Any]) -> tuple[str, list[AsrUtterance]]:
    """把 result 数组摊平；开启声道分轨时它可能有两项，按时间重新排一遍。"""
    texts: list[str] = []
    utterances: list[AsrUtterance] = []
    for channel in payload.get("result") or []:
        if not isinstance(channel, dict):
            continue
        if channel.get("text"):
            texts.append(str(channel["text"]))
        for raw in channel.get("utterances") or []:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text") or "").strip()
            if not text:
                continue
            speaker = raw.get("speaker") or {}
            utterances.append(
                AsrUtterance(
                    text=text,
                    start_ms=int(raw.get("start_time") or 0),
                    end_ms=int(raw.get("end_time") or 0),
                    speaker_id=str(speaker.get("id") or "").strip()
                    if isinstance(speaker, dict)
                    else "",
                )
            )
    utterances.sort(key=lambda u: (u.start_ms, u.end_ms))
    return "".join(texts), utterances


class StepAsrClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self._base_url = (base_url or settings.step_asr_base_url).rstrip("/")
        # 同一个平台的密钥，默认与大模型共用，允许单独覆盖
        self._api_key = api_key or settings.step_asr_api_key or settings.llm_api_key
        self._model = model or settings.step_asr_model

    @property
    def model(self) -> str:
        return self._model

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _post(
        self, client: httpx.AsyncClient, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        attempts = max(1, runtime_config.get_int("ASR_MAX_ATTEMPTS", 3))
        last_detail = ""
        last_status: int | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await client.post(
                    f"{self._base_url}{path}", headers=self._headers(), json=payload
                )
            except httpx.HTTPError as exc:
                last_status, last_detail = None, f"连接错误: {exc}"
            else:
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise AsrRequestError(f"转写服务返回的不是 JSON: {exc}") from exc
                last_status = response.status_code
                last_detail = response.text[:500]
                if response.status_code not in RETRYABLE_STATUS:
                    raise AsrRequestError(
                        f"转写服务拒绝请求 status={response.status_code}: {last_detail}",
                        status_code=response.status_code,
                    )
            logger.warning(
                "转写接口调用失败 path=%s status=%s（第 %d/%d 次），怀疑服务端瞬时故障。详情: %s",
                path,
                last_status,
                attempt,
                attempts,
                last_detail,
            )
            if attempt < attempts:
                await asyncio.sleep(min(5 * attempt, 20))
        raise AsrRequestError(
            f"转写接口连续 {attempts} 次不可用，最后一次 status={last_status}: {last_detail}",
            status_code=last_status,
        )

    async def submit(
        self,
        audio_url: str,
        *,
        audio_format: str = "mp3",
        channel: int = 1,
        show_utterances: bool = True,
        enable_speaker_info: bool = True,
    ) -> str:
        if not self._api_key:
            raise AsrRequestError("未配置转写密钥，无法调用语音识别")
        payload: dict[str, Any] = {
            "audio": {
                "format": audio_format,
                "channel": channel,
                "url": audio_url,
            },
            "request": {
                "model_name": self._model,
                "show_utterances": show_utterances,
                "enable_speaker_info": enable_speaker_info,
            },
        }
        timeout = httpx.Timeout(60.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await self._post(client, SUBMIT_PATH, payload)
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            raise AsrRequestError(f"转写服务没有返回 task_id，响应: {data}")
        logger.info("已提交转写任务 task_id=%s model=%s", task_id, self._model)
        return task_id

    async def query(self, task_id: str) -> dict[str, Any]:
        timeout = httpx.Timeout(60.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await self._post(client, QUERY_PATH, {"task_id": task_id})

    async def transcribe(
        self,
        audio_url: str,
        *,
        audio_format: str = "mp3",
        channel: int = 1,
        on_wait: Callable[[float], None] | None = None,
    ) -> AsrResult:
        """提交并等到出结果；on_wait 用于把等待时长回吐给进度条。"""
        started = time.perf_counter()
        task_id = await self.submit(
            audio_url, audio_format=audio_format, channel=channel
        )

        interval = max(1.0, runtime_config.get_float("ASR_POLL_INTERVAL_SECONDS", 3.0))
        max_wait = max(60.0, runtime_config.get_float("ASR_MAX_WAIT_SECONDS", 1800.0))
        while True:
            await asyncio.sleep(interval)
            data = await self.query(task_id)
            status = str(data.get("status") or "").strip().upper()
            if status == STATUS_FAILED:
                error = data.get("error") or {}
                stage = error.get("stage") if isinstance(error, dict) else ""
                message = error.get("message") if isinstance(error, dict) else ""
                raise AsrRequestError(
                    f"转写任务失败 task_id={task_id} 阶段={stage or '未知'}: {message or data}"
                )
            if status in {STATUS_PENDING, STATUS_RUNNING}:
                waited = time.perf_counter() - started
                if waited > max_wait:
                    raise AsrRequestError(
                        f"转写任务超过 {max_wait:.0f}s 仍未完成 task_id={task_id}，已放弃等待"
                    )
                if on_wait is not None:
                    on_wait(waited)
                continue

            # 成功时响应里没有 status 字段，直接给 duration 与 result
            text, utterances = _parse_result(data)
            if not utterances and not text:
                # 任务本身成功，只是这段没人说话；当失败处理会让上层白白重切重试
                logger.warning(
                    "转写任务没有识别到语音 task_id=%s，按空结果返回。响应: %s",
                    task_id,
                    str(data)[:300],
                )
            elapsed = time.perf_counter() - started
            result = AsrResult(
                utterances=utterances,
                text=text,
                duration_seconds=float(data.get("duration") or 0.0),
                task_id=task_id,
                elapsed_seconds=elapsed,
            )
            logger.info(
                "转写完成 task_id=%s 音频 %.0fs / 分句 %d 条 / 说话人 %d 位 / 用时 %.0fs",
                task_id,
                result.duration_seconds,
                len(result.utterances),
                len(result.speaker_ids),
                elapsed,
            )
            return result
