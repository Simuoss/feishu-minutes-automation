"""纪要生成链路的端到端验证（注入假模型，不产生真实调用）。"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.integrations.llm.messages_client import LlmCompletion, LlmRequestError
from app.service.meeting_storage_service import MeetingStorageService
from app.service.summary_event_broker import SummaryStatus, summary_broker
from app.service.summary_generation_service import SummaryGenerationService

TOKEN = "obtest0001"
OWNER = 1


@pytest.fixture(autouse=True)
def _disable_r2_for_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.service.r2_media_service.R2MediaService.enabled",
        lambda self: False,
    )

TRANSCRIPT = """2026-07-21 20:58:04 CST|1小时 3秒

关键词:
向量、链路

同学甲 00:00:11.161
讲解 token 的概念。

同学乙 00:05:08.720
提了一个问题。

同学甲 00:34:52.003
讲解向量模型。
"""

# 第二个锚点被模型取整成了 00:35:00，转写中并不存在该时间点
MODEL_OUTPUT = """# 测试课程

## 知识主干

### token `00:00:11`
内容。

### 向量模型 `00:35:00`
内容。
"""


SCENE_LECTURE = json.dumps(
    {"scene": "LECTURE", "reason": "两人，一方占九成发言，全程在讲概念"},
    ensure_ascii=False,
)

SCENE_MEETING = json.dumps(
    {"scene": "MEETING", "reason": "三人发言分布分散，在过进度并派活"},
    ensure_ascii=False,
)


def _is_scene_prompt(system_prompt: str) -> bool:
    """场景判定那次调用的提示词，用它路由假模型的返回。"""
    return "LECTURE" in str(system_prompt) and "MEETING" in str(system_prompt)


class FakeLlm:
    def __init__(
        self,
        text: str | None = None,
        error: Exception | None = None,
        scene: str = SCENE_LECTURE,
    ) -> None:
        self._text = text
        self._error = error
        self._scene = scene
        self.calls = 0
        self.scene_calls = 0
        self.write_prompts: list[str] = []

    async def complete(
        self,
        system_prompt,
        user_content,
        *,
        on_attempt=None,
        on_delta=None,
        on_restart=None,
        **kwargs,
    ):
        if _is_scene_prompt(system_prompt):
            self.scene_calls += 1
            return LlmCompletion(
                text=self._scene,
                input_tokens=200,
                output_tokens=30,
                stop_reason="end_turn",
                attempts=1,
                elapsed_seconds=1.0,
            )
        self.calls += 1
        self.write_prompts.append(str(system_prompt))
        if on_attempt is not None:
            on_attempt(1, 1)
        if self._error is not None:
            raise self._error
        text = self._text or ""
        if on_delta is not None:
            # 模拟流式：分片推送
            for i in range(0, len(text), 20):
                on_delta(text[i : i + 20])
        return LlmCompletion(
            text=text,
            input_tokens=1000,
            output_tokens=500,
            stop_reason="end_turn",
            attempts=1,
            elapsed_seconds=12.3,
        )


def _make_storage(tmp_path: Path, *, with_transcript: bool = True) -> MeetingStorageService:
    storage = MeetingStorageService(storage_root=str(tmp_path))
    if with_transcript:
        storage.ensure_layout(TOKEN, owner_user_id=OWNER)
        storage.save_transcript(
            TOKEN, TRANSCRIPT.encode("utf-8"), owner_user_id=OWNER
        )
    return storage


def test_generate_writes_summary_and_snaps_anchor(tmp_path: Path):
    summary_broker.clear(TOKEN, owner_user_id=OWNER)
    storage = _make_storage(tmp_path)
    service = SummaryGenerationService(storage=storage, llm=FakeLlm(MODEL_OUTPUT))

    result = asyncio.run(service.generate(TOKEN, owner_user_id=OWNER))

    assert result.status == SummaryStatus.COMPLETED.value
    assert result.anchor_total == 2
    assert result.anchor_aligned == 1

    saved = storage.read_summary(TOKEN, owner_user_id=OWNER)
    assert saved is not None
    # 取整的 00:35:00 应被吸附到真实存在的 00:34:52
    assert "`00:34:52`" in saved["content"]
    assert "`00:35:00`" not in saved["content"]

    meta = saved["meta"]
    assert meta["anchor_aligned"] == 1
    assert meta["input_tokens"] == 1000
    assert meta["truncated"] is False


def test_detected_scene_selects_prompt_and_lands_in_meta(tmp_path: Path):
    summary_broker.clear(TOKEN, owner_user_id=OWNER)
    storage = _make_storage(tmp_path)
    llm = FakeLlm(MODEL_OUTPUT, scene=SCENE_MEETING)

    asyncio.run(SummaryGenerationService(storage=storage, llm=llm).generate(
        TOKEN, owner_user_id=OWNER
    ))

    assert llm.scene_calls == 1
    # 判定为会议就必须换成会议模板，不能仍按讲课那套写
    assert "会议纪要" in llm.write_prompts[0]
    assert "课后复习" not in llm.write_prompts[0]

    meta = storage.read_summary(TOKEN, owner_user_id=OWNER)["meta"]
    assert meta["scene"] == "MEETING"
    assert "三人" in meta["scene_reason"]


def test_write_mode_reuses_previous_scene(tmp_path: Path):
    """重跑成文不该再花一次调用去判定同一份转写。"""
    summary_broker.clear(TOKEN, owner_user_id=OWNER)
    storage = _make_storage(tmp_path)
    llm = FakeLlm(MODEL_OUTPUT, scene=SCENE_MEETING)
    service = SummaryGenerationService(storage=storage, llm=llm)

    asyncio.run(service.generate(TOKEN, owner_user_id=OWNER))
    asyncio.run(service.generate(TOKEN, owner_user_id=OWNER, mode="WRITE", force=True))

    assert llm.scene_calls == 1
    assert llm.calls == 2
    assert storage.read_summary(TOKEN, owner_user_id=OWNER)["meta"]["scene"] == "MEETING"


def test_unrecognized_scene_aborts_without_writing(tmp_path: Path):
    """判定失败宁可整场失败，也不猜一套模板写下去。"""
    summary_broker.clear(TOKEN, owner_user_id=OWNER)
    storage = _make_storage(tmp_path)
    llm = FakeLlm(
        MODEL_OUTPUT,
        scene=json.dumps({"scene": "TRAINING", "reason": "像培训"}, ensure_ascii=False),
    )

    result = asyncio.run(SummaryGenerationService(storage=storage, llm=llm).generate(
        TOKEN, owner_user_id=OWNER
    ))

    assert result.status == SummaryStatus.FAILED.value
    assert "场景判定失败" in (result.error_message or "")
    # 不该继续浪费一次成文调用，也不该留下半成品
    assert llm.calls == 0
    assert storage.read_summary(TOKEN, owner_user_id=OWNER) is None


def test_non_json_scene_output_aborts(tmp_path: Path):
    summary_broker.clear(TOKEN, owner_user_id=OWNER)
    storage = _make_storage(tmp_path)
    llm = FakeLlm(MODEL_OUTPUT, scene="这应该是一节课吧")

    result = asyncio.run(SummaryGenerationService(storage=storage, llm=llm).generate(
        TOKEN, owner_user_id=OWNER
    ))

    assert result.status == SummaryStatus.FAILED.value
    channel = summary_broker.get(TOKEN, owner_user_id=OWNER)
    assert channel is not None
    assert channel.status == SummaryStatus.FAILED.value
    assert llm.calls == 0


def test_generate_skips_when_summary_exists(tmp_path: Path):
    summary_broker.clear(TOKEN, owner_user_id=OWNER)
    storage = _make_storage(tmp_path)
    llm = FakeLlm(MODEL_OUTPUT)
    service = SummaryGenerationService(storage=storage, llm=llm)

    asyncio.run(service.generate(TOKEN, owner_user_id=OWNER))
    asyncio.run(service.generate(TOKEN, owner_user_id=OWNER))
    assert llm.calls == 1

    asyncio.run(service.generate(TOKEN, owner_user_id=OWNER, force=True))
    assert llm.calls == 2


def test_generate_fails_fast_without_transcript(tmp_path: Path):
    summary_broker.clear(TOKEN, owner_user_id=OWNER)
    storage = _make_storage(tmp_path, with_transcript=False)
    llm = FakeLlm(MODEL_OUTPUT)
    service = SummaryGenerationService(storage=storage, llm=llm)

    result = asyncio.run(service.generate(TOKEN, owner_user_id=OWNER))

    assert result.status == SummaryStatus.FAILED.value
    assert result.error_message is not None
    assert "转写" in result.error_message
    # 转写缺失时不应浪费一次模型调用
    assert llm.calls == 0


def test_model_failure_is_reported_as_failed(tmp_path: Path):
    summary_broker.clear(TOKEN, owner_user_id=OWNER)
    storage = _make_storage(tmp_path)
    service = SummaryGenerationService(
        storage=storage,
        llm=FakeLlm(error=LlmRequestError("模型服务连续 5 次不可用", status_code=500, attempts=5)),
    )

    result = asyncio.run(service.generate(TOKEN, owner_user_id=OWNER))

    assert result.status == SummaryStatus.FAILED.value
    channel = summary_broker.get(TOKEN, owner_user_id=OWNER)
    assert channel is not None
    assert channel.status == SummaryStatus.FAILED.value
    assert storage.read_summary(TOKEN, owner_user_id=OWNER) is None


def test_streaming_deltas_are_broadcast_to_subscriber(tmp_path: Path):
    summary_broker.clear(TOKEN, owner_user_id=OWNER)
    storage = _make_storage(tmp_path)
    service = SummaryGenerationService(storage=storage, llm=FakeLlm(MODEL_OUTPUT))

    async def scenario():
        received: list[dict[str, Any]] = []

        async def listen():
            async for item in summary_broker.subscribe(TOKEN, owner_user_id=OWNER):
                received.append(item)
                if item["event"] in {"done", "failed"}:
                    return

        listener = asyncio.create_task(listen())
        await asyncio.sleep(0)  # 让订阅先建立，避免漏掉首批事件
        await service.generate(TOKEN, owner_user_id=OWNER)
        await asyncio.wait_for(listener, timeout=5)
        return received

    events = asyncio.run(scenario())
    kinds = [e["event"] for e in events]

    assert kinds[0] == "snapshot"
    assert "delta" in kinds
    assert kinds[-1] == "done"

    streamed = "".join(e["data"]["text"] for e in events if e["event"] == "delta")
    assert streamed == MODEL_OUTPUT

    done = events[-1]["data"]
    # 落盘与推送给前端的最终版本都应是锚点校正后的内容
    assert "`00:34:52`" in done["content"]
    assert done["meta"]["anchor_aligned"] == 1
