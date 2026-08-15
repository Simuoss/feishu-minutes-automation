"""划词答疑 Agent：基于选中片段 + 附近上下文的多轮多模态对话。"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from app.integrations.llm.messages_client import (
    AnthropicMessagesClient,
    LlmImage,
    LlmRequestError,
)
from app.service.meeting_storage_service import MeetingStorageService

logger = logging.getLogger(__name__)

CONTEXT_RADIUS = 300
MAX_IMAGE_BYTES = 4 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}

SYSTEM_PROMPT = """你是飞书妙记课程答疑助手。用户从会议纪要或转写中划选了一段文字并向你提问。

规则：
1. 只依据用户消息中给出的「划选片段」与「附近上下文」作答；不要编造文档中没有的课程事实。
2. 若信息不足，明确说明缺什么，并基于已有片段做有限推断。
3. 回答简洁、结构化，可用短列表；必要时引用原句。
4. 用户附图时，结合图片与文本片段一起理解。
5. 你不是整场会议的权威记录，回答应体现「基于选中片段」。"""


@dataclass
class PassageAskResult:
    reply: str
    user_message: str
    truncated: bool
    input_tokens: int
    output_tokens: int
    thinking: str = ""
    has_thinking: bool = False


@dataclass
class _PreparedAsk:
    user_text: str
    messages: list[dict]


class PassageAskService:
    def __init__(self) -> None:
        self._storage = MeetingStorageService()
        self._llm = AnthropicMessagesClient()

    async def ask(
        self,
        *,
        minute_token: str,
        owner_user_id: int,
        source_kind: str,
        selected_text: str,
        history: list[dict],
        question: str | None,
        images: list[dict],
        on_delta: Callable[[str], None] | None = None,
        on_thinking_start: Callable[[], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        on_restart: Callable[[], None] | None = None,
    ) -> PassageAskResult:
        prepared = await self._prepare(
            minute_token=minute_token,
            owner_user_id=owner_user_id,
            source_kind=source_kind,
            selected_text=selected_text,
            history=history,
            question=question,
            images=images,
        )
        try:
            # 划词答疑偏短问快答：思考强度固定最低
            completion = await self._llm.complete_messages(
                SYSTEM_PROMPT,
                prepared.messages,
                effort="low",
                on_delta=on_delta,
                on_thinking_start=on_thinking_start,
                on_thinking_delta=on_thinking_delta,
                on_restart=on_restart,
            )
        except LlmRequestError as exc:
            logger.error(
                "划词答疑调用大模型失败 token=%s kind=%s：%s",
                minute_token,
                (source_kind or "").strip().upper(),
                exc,
            )
            raise RuntimeError(str(exc)) from exc

        return PassageAskResult(
            reply=completion.text.strip(),
            user_message=prepared.user_text,
            truncated=completion.truncated,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            thinking=completion.thinking or "",
            has_thinking=completion.has_thinking,
        )

    async def iter_ask_events(
        self,
        *,
        minute_token: str,
        owner_user_id: int,
        source_kind: str,
        selected_text: str,
        history: list[dict],
        question: str | None,
        images: list[dict],
    ) -> AsyncIterator[tuple[str, dict]]:
        """SSE 事件流：(event_name, data_dict)。"""
        t0 = time.perf_counter()
        prepared = await self._prepare(
            minute_token=minute_token,
            owner_user_id=owner_user_id,
            source_kind=source_kind,
            selected_text=selected_text,
            history=history,
            question=question,
            images=images,
        )
        prep_s = time.perf_counter() - t0
        logger.info(
            "划词答疑本地准备完成 token=%s prep=%.2fs；随后耗时主要在上游模型首包",
            minute_token,
            prep_s,
        )

        queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()
        t_llm = time.perf_counter()
        first_signal_logged = False

        def _elapsed() -> float:
            return round(time.perf_counter() - t_llm, 3)

        def on_delta(chunk: str) -> None:
            nonlocal first_signal_logged
            if not first_signal_logged:
                first_signal_logged = True
                logger.info(
                    "划词答疑收到正文首包 token=%s ttft=%.2fs（含上游思考）",
                    minute_token,
                    time.perf_counter() - t_llm,
                )
                queue.put_nowait(
                    ("status", {"stage": "ANSWERING", "elapsed_seconds": _elapsed()})
                )
            queue.put_nowait(("delta", {"text": chunk}))

        def on_thinking_start() -> None:
            nonlocal first_signal_logged
            if not first_signal_logged:
                first_signal_logged = True
                logger.info(
                    "划词答疑进入模型思考块 token=%s wait=%.2fs",
                    minute_token,
                    time.perf_counter() - t_llm,
                )
            queue.put_nowait(
                ("status", {"stage": "THINKING", "elapsed_seconds": _elapsed()})
            )
            queue.put_nowait(("thinking_start", {}))

        def on_thinking_delta(chunk: str) -> None:
            queue.put_nowait(("thinking", {"text": chunk}))

        def on_restart() -> None:
            nonlocal first_signal_logged
            first_signal_logged = False
            queue.put_nowait(("restart", {}))
            queue.put_nowait(
                ("status", {"stage": "WAITING_MODEL", "elapsed_seconds": _elapsed()})
            )

        async def run() -> None:
            try:
                completion = await self._llm.complete_messages(
                    SYSTEM_PROMPT,
                    prepared.messages,
                    effort="low",
                    on_delta=on_delta,
                    on_thinking_start=on_thinking_start,
                    on_thinking_delta=on_thinking_delta,
                    on_restart=on_restart,
                )
                logger.info(
                    "划词答疑模型完成 token=%s total=%.2fs in=%s out=%s has_thinking=%s",
                    minute_token,
                    time.perf_counter() - t_llm,
                    completion.input_tokens,
                    completion.output_tokens,
                    completion.has_thinking,
                )
                queue.put_nowait(
                    (
                        "done",
                        {
                            "reply": completion.text.strip(),
                            "user_message": prepared.user_text,
                            "truncated": completion.truncated,
                            "input_tokens": completion.input_tokens,
                            "output_tokens": completion.output_tokens,
                            "thinking": completion.thinking or "",
                            "has_thinking": completion.has_thinking,
                        },
                    )
                )
            except LlmRequestError as exc:
                logger.error(
                    "划词答疑流式调用大模型失败 token=%s wait=%.2fs：%s",
                    minute_token,
                    time.perf_counter() - t_llm,
                    exc,
                )
                queue.put_nowait(("error", {"detail": str(exc)}))
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "划词答疑流式生成异常 token=%s，怀疑上游或本地逻辑故障：%s",
                    minute_token,
                    exc,
                )
                queue.put_nowait(("error", {"detail": "提问失败，请稍后重试"}))
            finally:
                queue.put_nowait(None)

        # 先启动模型调用，再推 meta，避免等客户端读完首包才占槽/建连
        task = asyncio.create_task(run())
        yield ("meta", {"user_message": prepared.user_text})
        yield (
            "status",
            {
                "stage": "WAITING_MODEL",
                "prep_seconds": round(prep_s, 3),
                "elapsed_seconds": 0,
            },
        )
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            await task

    async def _prepare(
        self,
        *,
        minute_token: str,
        owner_user_id: int,
        source_kind: str,
        selected_text: str,
        history: list[dict],
        question: str | None,
        images: list[dict],
    ) -> _PreparedAsk:
        kind = (source_kind or "").strip().upper()
        if kind not in {"SUMMARY", "TRANSCRIPT"}:
            raise ValueError("source_kind 只能是 SUMMARY 或 TRANSCRIPT")

        selected = (selected_text or "").strip()
        if not selected:
            raise ValueError("请先划选一段文字")

        source = await self._load_source(
            minute_token, owner_user_id=owner_user_id, kind=kind
        )
        if not self._selection_in_source(selected, source):
            raise PermissionError("划选内容不属于当前文章，请在正文中重新划选")

        context = self._nearby_context(source, selected)
        llm_images = self._parse_images(images)
        is_first = not history

        if is_first:
            user_text = self._build_first_user_message(selected, context, question)
        else:
            q = (question or "").strip()
            if not q and not llm_images:
                raise ValueError("请输入追问内容或附上图片")
            user_text = q or "请结合附图继续说明。"

        messages = self._history_to_messages(history)
        messages.append(self._user_message_payload(user_text, llm_images))
        return _PreparedAsk(user_text=user_text, messages=messages)

    async def _load_source(
        self, minute_token: str, *, owner_user_id: int, kind: str
    ) -> str:
        if kind == "SUMMARY":
            detail = await self._storage.read_summary_async(
                minute_token, owner_user_id=owner_user_id
            )
            if detail is None:
                raise LookupError("该会议尚未生成纪要")
            text = (detail.get("content") or "").strip()
            if not text:
                raise LookupError("纪要正文为空")
            # 用户划的是页面上的真名，喂给模型的上下文也得是真名，否则对不上
            from app.service import speaker_naming_service

            return await speaker_naming_service.apply_speaker_names_to_summary(
                text, minute_token, owner_user_id=owner_user_id
            )
        text = await self._storage.read_transcript_async(
            minute_token, owner_user_id=owner_user_id
        )
        if text is None or not str(text).strip():
            raise LookupError("本地没有该会议的转写文本")
        return str(text)

    @staticmethod
    def _normalize_ws(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    @classmethod
    def _plainish(cls, text: str) -> str:
        """去掉常见 Markdown 记号后再比，适配渲染页划选。"""
        t = text or ""
        t = re.sub(r"!\[.*?\]\(.*?\)", " ", t)
        t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
        t = re.sub(r"[#*_`~>|]+", " ", t)
        return cls._normalize_ws(t)

    def _selection_in_source(self, selected: str, source: str) -> bool:
        if selected in source:
            return True
        # 划选可能跨 HTML/空白差异，用压缩空白再比一次
        if self._normalize_ws(selected) in self._normalize_ws(source):
            return True
        # 纪要经 Markdown 渲染后，划选往往不含 # / ** 等记号
        return self._plainish(selected) in self._plainish(source)

    def _nearby_context(self, source: str, selected: str) -> str:
        idx = source.find(selected)
        if idx < 0:
            norm_source = self._normalize_ws(source)
            norm_sel = self._normalize_ws(selected)
            nidx = norm_source.find(norm_sel)
            if nidx < 0:
                return selected
            # 无法可靠映射回原文偏移时，至少返回选区本身
            return selected
        start = max(0, idx - CONTEXT_RADIUS)
        end = min(len(source), idx + len(selected) + CONTEXT_RADIUS)
        return source[start:end]

    @staticmethod
    def _build_first_user_message(
        selected: str, context: str, question: str | None
    ) -> str:
        q = (question or "").strip()
        ask_line = q if q else "请解释这段在讲什么，并补充理解要点。"
        return (
            "【划选片段】\n"
            f"{selected}\n\n"
            "【附近上下文（含划选前后约 300 字）】\n"
            f"{context}\n\n"
            f"【问题】\n{ask_line}"
        )

    def _parse_images(self, images: list[dict]) -> list[LlmImage]:
        parsed: list[LlmImage] = []
        for raw in images or []:
            media_type = (raw.get("media_type") or "").strip().lower()
            if media_type == "image/jpg":
                media_type = "image/jpeg"
            if media_type not in ALLOWED_IMAGE_TYPES:
                raise ValueError(f"不支持的图片类型: {media_type or '(空)'}")
            b64 = (raw.get("data_base64") or "").strip()
            if not b64:
                raise ValueError("图片数据为空")
            if "," in b64 and b64.lower().startswith("data:"):
                b64 = b64.split(",", 1)[1]
            try:
                data = base64.b64decode(b64, validate=False)
            except Exception as exc:  # noqa: BLE001
                raise ValueError("图片 base64 无法解码") from exc
            if not data:
                raise ValueError("图片数据为空")
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("单张图片不能超过 4MB")
            parsed.append(LlmImage(data=data, media_type=media_type))
        return parsed

    def _history_to_messages(self, history: list[dict]) -> list[dict]:
        messages: list[dict] = []
        for item in history or []:
            role = (item.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                raise ValueError("history.role 只能是 user 或 assistant")
            content = item.get("content") or ""
            imgs = self._parse_images(item.get("images") or [])
            if role == "assistant":
                messages.append({"role": "assistant", "content": str(content)})
            else:
                messages.append(self._user_message_payload(str(content), imgs))
        return messages

    @staticmethod
    def _user_message_payload(text: str, images: list[LlmImage]) -> dict:
        if not images:
            return {"role": "user", "content": text}
        blocks: list[dict] = [img.to_block() for img in images]
        blocks.append({"type": "text", "text": text})
        return {"role": "user", "content": blocks}


passage_ask_service = PassageAskService()
