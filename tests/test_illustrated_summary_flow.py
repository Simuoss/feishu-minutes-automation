"""配图纪要流水线的端到端验证（假模型 + 假 ffmpeg，不产生真实调用）。

要守住的核心行为：写作那一次调用必须同时拿到转写和图片本身，
而不是先把图片转成文字再成文。
"""

import asyncio
import json
import random
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from app.integrations.llm.messages_client import LlmCompletion, LlmImage
from app.integrations.video.ffmpeg_client import FfmpegError, VideoInfo
from app.service.meeting_storage_service import MeetingStorageService
from app.service.summary_event_broker import SummaryStatus, summary_broker
from app.service.summary_generation_service import SummaryGenerationService

TOKEN = "obillus0001"

TRANSCRIPT = """2026-07-21 20:58:04 CST|1小时 3秒

关键词:
安装、环境

同学甲 00:00:11.161
今天先把环境装上。

同学甲 00:02:52.400
你看这里报错了，路径不对。

同学甲 00:34:52.003
这四个都打上勾。
"""

NO_SHARE_PLAN = json.dumps(
    {
        "screen_shared": False,
        "reason": "样本帧全程是参会者头像宫格，看不到桌面或幻灯片",
        "figures": [],
    },
    ensure_ascii=False,
)

PLAN_OUTPUT = json.dumps(
    {
        "screen_shared": True,
        "reason": "样本帧里能看到 IDE 和终端窗口，不是摄像头宫格",
        "figures": [
            {
                "timestamp": "00:02:52",
                "expect": "终端里的 pip 报错",
                "purpose": "说明路径不对",
                "priority": 1,
            },
            {
                "timestamp": "00:34:52",
                "expect": "安装器的四个勾选项",
                "purpose": "说明该勾哪些",
                "priority": 1,
            },
        ]
    },
    ensure_ascii=False,
)

# 成文前脱敏扫描：测试默认两张都无敏感信息
SCAN_CLEAN = json.dumps(
    {
        "figures": [
            {"figure_id": "fig-01", "sensitive": False, "regions": []},
            {"figure_id": "fig-02", "sensitive": False, "regions": []},
        ]
    },
    ensure_ascii=False,
)

# 模型只用了第一张图，第二张应被删除
WRITE_OUTPUT = """# 环境安装

## 知识主干

### 装错了环境 `00:02:52`

pip 指向了旧解释器。

![终端里的 pip 报错](assets/fig-01.jpg)
图：路径落在了 `python37` 下 `00:02:52`

### 勾选项 `00:34:52`

四个都要勾上。
"""


class ScriptedLlm:
    """按调用顺序返回预设输出，并记录每次调用的入参。"""

    def __init__(self, *outputs: str) -> None:
        self._outputs = list(outputs)
        self.calls: list[dict[str, object]] = []

    async def complete(
        self,
        system_prompt,
        user_content,
        *,
        images=None,
        on_attempt=None,
        on_delta=None,
        on_restart=None,
        **kwargs,
    ):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_content": user_content,
                "images": list(images or []),
            }
        )
        if on_attempt is not None:
            on_attempt(1, 1)
        text = self._outputs.pop(0) if self._outputs else ""
        if on_delta is not None:
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


def _paint_frame(path: Path, seed: float) -> None:
    """画一张由时间点决定的假截图。

    用确定性随机填充粗网格：同一时间点画面完全一致，不同时间点指纹相距约一半比特，
    这样才测得出去重逻辑是"按画面内容"而不是"按时间"工作的。
    """
    rng = random.Random(int(seed))
    image = Image.new("RGB", (640, 360), (12, 12, 12))
    draw = ImageDraw.Draw(image)
    cols, rows = 20, 12
    cell_w, cell_h = 640 // cols, 360 // rows
    for row in range(rows):
        for col in range(cols):
            tone = rng.randrange(0, 256)
            draw.rectangle(
                [col * cell_w, row * cell_h, (col + 1) * cell_w, (row + 1) * cell_h],
                fill=(tone, tone, tone),
            )
    image.save(path, "JPEG", quality=95)


class FakeFfmpeg:
    """产出可被 Pillow 读取的假帧，每个时间点画面不同以避免被判为重复。"""

    def __init__(
        self,
        *,
        probe_error: bool = False,
        width: int = 1280,
    ) -> None:
        self._probe_error = probe_error
        self._width = width
        self.burst_calls: list[float] = []

    async def probe(self, video_path: Path) -> VideoInfo:
        if self._probe_error:
            raise FfmpegError("ffprobe 探测失败 code=1，怀疑文件不是有效视频")
        return VideoInfo(
            path=video_path,
            duration_seconds=3600.0,
            width=self._width,
            height=720,
            codec="h264",
        )

    async def extract_burst(
        self,
        video_path: Path,
        base_seconds: float,
        offsets: list[float],
        dest_dir: Path,
        prefix: str,
        *,
        duration_limit: float | None = None,
    ) -> list[tuple[float, Path]]:
        self.burst_calls.append(base_seconds)
        dest_dir.mkdir(parents=True, exist_ok=True)
        results: list[tuple[float, Path]] = []
        for index, offset in enumerate(offsets):
            moment = max(0.0, base_seconds + offset)
            path = dest_dir / f"{prefix}_{index}.jpg"
            _paint_frame(path, moment)
            results.append((moment, path))
        return results


def _make_storage(tmp_path: Path, *, media_name: str | None = "lecture.mp4") -> MeetingStorageService:
    storage = MeetingStorageService(storage_root=str(tmp_path))
    layout = storage.ensure_layout(TOKEN)
    storage.save_transcript(TOKEN, TRANSCRIPT.encode("utf-8"))
    if media_name:
        # save_media 会走真实下载，这里直接把文件摆好
        (layout["media"] / media_name).write_bytes(b"fake-media-bytes")
    return storage


def _service(storage, llm, ffmpeg) -> SummaryGenerationService:
    return SummaryGenerationService(storage=storage, llm=llm, ffmpeg=ffmpeg)


def test_writing_call_receives_transcript_and_images_together(tmp_path: Path):
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path)
    llm = ScriptedLlm(PLAN_OUTPUT, SCAN_CLEAN, WRITE_OUTPUT)

    result = asyncio.run(_service(storage, llm, FakeFfmpeg()).generate(TOKEN))

    assert result.status == SummaryStatus.COMPLETED.value
    # 三次调用：规划截图 -> 敏感扫描 -> 看图成文
    assert len(llm.calls) == 3

    plan_call, scan_call, write_call = llm.calls
    # 规划调用必须带上开场抽帧样本，供模型视觉判断是否共享屏幕
    plan_images = plan_call["images"]
    assert isinstance(plan_images, list)
    assert len(plan_images) >= 2
    assert all(isinstance(img, LlmImage) for img in plan_images)
    assert "<transcript>" in str(plan_call["user_content"])
    assert "只根据这些画面" in str(plan_call["user_content"])

    assert len(scan_call["images"]) == 2
    assert "敏感" in str(scan_call["system_prompt"]) or "扫描" in str(scan_call["user_content"])

    images = write_call["images"]
    assert isinstance(images, list)
    assert len(images) == 2
    assert all(isinstance(img, LlmImage) for img in images)
    # 同一次请求里既有图片也有转写全文，模型自己看图取舍
    assert "<transcript>" in str(write_call["user_content"])
    assert "<figures>" in str(write_call["user_content"])
    assert "fig-01" in str(write_call["user_content"])
    assert "配图" in str(write_call["system_prompt"])


def test_unreferenced_figure_is_deleted_from_disk(tmp_path: Path):
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path)
    llm = ScriptedLlm(PLAN_OUTPUT, SCAN_CLEAN, WRITE_OUTPUT)

    result = asyncio.run(_service(storage, llm, FakeFfmpeg()).generate(TOKEN))

    assets = storage.get_assets_dir(TOKEN)
    assert (assets / "fig-01.jpg").is_file()
    assert not (assets / "fig-02.jpg").exists()
    assert result.figure_planned == 2
    assert result.figure_used == 1

    meta = storage.read_summary(TOKEN)["meta"]
    assert meta["figure_used"] == 1


def test_candidate_frames_are_cleaned_up(tmp_path: Path):
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path)
    llm = ScriptedLlm(PLAN_OUTPUT, SCAN_CLEAN, WRITE_OUTPUT)

    asyncio.run(_service(storage, llm, FakeFfmpeg()).generate(TOKEN))

    # 中间候选帧不该留在磁盘上，也不该出现在对外暴露的 assets 目录里
    assert not storage.get_frames_work_dir(TOKEN).exists()
    names = {f.name for f in storage.get_assets_dir(TOKEN).iterdir()}
    assert names == {"fig-01.jpg"}


def test_audio_only_meeting_falls_back_to_text_summary(tmp_path: Path):
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path, media_name="lecture.mp3")
    llm = ScriptedLlm(WRITE_OUTPUT)

    result = asyncio.run(_service(storage, llm, FakeFfmpeg()).generate(TOKEN))

    assert result.status == SummaryStatus.COMPLETED.value
    # 没有视频就不该有规划截图那一次调用
    assert len(llm.calls) == 1
    assert llm.calls[0]["images"] == []
    assert result.figure_planned == 0


def test_probe_failure_degrades_to_text_summary(tmp_path: Path):
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path)
    llm = ScriptedLlm(WRITE_OUTPUT)

    result = asyncio.run(
        _service(storage, llm, FakeFfmpeg(probe_error=True)).generate(TOKEN)
    )

    assert result.status == SummaryStatus.COMPLETED.value
    assert len(llm.calls) == 1
    assert result.figure_used == 0
    assert storage.read_summary(TOKEN) is not None


def test_empty_plan_degrades_to_text_summary(tmp_path: Path):
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path)
    llm = ScriptedLlm('{"figures": []}', WRITE_OUTPUT)

    result = asyncio.run(_service(storage, llm, FakeFfmpeg()).generate(TOKEN))

    assert result.status == SummaryStatus.COMPLETED.value
    assert len(llm.calls) == 2
    assert llm.calls[1]["images"] == []
    assert result.figure_planned == 0


def test_non_json_plan_output_degrades_to_text_summary(tmp_path: Path):
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path)
    llm = ScriptedLlm("这节课没什么需要截图的。", WRITE_OUTPUT)

    result = asyncio.run(_service(storage, llm, FakeFfmpeg()).generate(TOKEN))

    assert result.status == SummaryStatus.COMPLETED.value
    assert llm.calls[1]["images"] == []
    assert result.figure_used == 0


def test_identical_sample_frames_still_ask_model(tmp_path: Path):
    """画面是否共享只由模型看抽帧判断，算法不再因帧相似而跳过规划。"""
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path)
    llm = ScriptedLlm(NO_SHARE_PLAN, WRITE_OUTPUT)

    result = asyncio.run(_service(storage, llm, FakeFfmpeg()).generate(TOKEN))

    assert result.status == SummaryStatus.COMPLETED.value
    assert len(llm.calls) == 2
    assert len(llm.calls[0]["images"]) >= 2
    assert llm.calls[1]["images"] == []
    assert result.figure_planned == 0


def test_plan_candidates_are_streamed_to_subscribers(tmp_path: Path):
    """规划阶段每挑中一处就该推一条，前端才不用干等几十秒。"""
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path)
    service = _service(
        storage, ScriptedLlm(PLAN_OUTPUT, SCAN_CLEAN, WRITE_OUTPUT), FakeFfmpeg()
    )

    async def scenario():
        received: list[dict[str, Any]] = []

        async def listen():
            async for item in summary_broker.subscribe(TOKEN):
                received.append(item)
                if item["event"] in {"done", "failed"}:
                    return

        listener = asyncio.create_task(listen())
        await asyncio.sleep(0)
        await service.generate(TOKEN)
        await asyncio.wait_for(listener, timeout=5)
        return received

    events = asyncio.run(scenario())
    plans = [e["data"] for e in events if e["event"] == "plan"]

    assert [p["timestamp"] for p in plans] == ["00:02:52", "00:34:52"]
    assert plans[0]["expect"] == "终端里的 pip 报错"
    assert [p["index"] for p in plans] == [1, 2]

    kinds = [e["event"] for e in events]
    # 候选必须先于正文出现，否则等于没起到"提前告知"的作用
    assert kinds.index("plan") < kinds.index("delta")


def test_snapshot_carries_plan_items_for_late_subscribers(tmp_path: Path):
    summary_broker.clear(TOKEN)
    summary_broker.add_plan_item(TOKEN, "00:02:52", "终端里的 pip 报错")

    async def scenario():
        async for item in summary_broker.subscribe(TOKEN):
            return item

    first = asyncio.run(scenario())
    assert first is not None
    assert first["event"] == "snapshot"
    assert first["data"]["plan_items"][0]["timestamp"] == "00:02:52"
    summary_broker.clear(TOKEN)


def test_model_verdict_of_no_screen_share_skips_illustration(tmp_path: Path):
    """开场抽帧差异够大，但模型看样本帧判定无共享屏幕时不配图。"""
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path)
    llm = ScriptedLlm(NO_SHARE_PLAN, WRITE_OUTPUT)

    result = asyncio.run(_service(storage, llm, FakeFfmpeg()).generate(TOKEN))

    assert result.status == SummaryStatus.COMPLETED.value
    # 规划那次调用发生了（带抽帧样本），但写作那次不该带图
    assert len(llm.calls) == 2
    assert len(llm.calls[0]["images"]) >= 2
    assert llm.calls[1]["images"] == []
    assert "配图" not in str(llm.calls[1]["system_prompt"])
    assert result.figure_planned == 0
    assert storage.read_summary(TOKEN) is not None


def test_no_screen_share_leaves_no_assets_on_disk(tmp_path: Path):
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path)
    llm = ScriptedLlm(NO_SHARE_PLAN, WRITE_OUTPUT)

    asyncio.run(_service(storage, llm, FakeFfmpeg()).generate(TOKEN))

    assets = storage.get_assets_dir(TOKEN)
    assert not assets.is_dir() or not any(assets.iterdir())


def test_screen_activity_check_cleans_up_samples(tmp_path: Path):
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path)
    llm = ScriptedLlm(PLAN_OUTPUT, SCAN_CLEAN, WRITE_OUTPUT)

    asyncio.run(_service(storage, llm, FakeFfmpeg()).generate(TOKEN))

    assert not storage.get_frames_work_dir(TOKEN).exists()


def test_regenerate_clears_previous_assets(tmp_path: Path):
    summary_broker.clear(TOKEN)
    storage = _make_storage(tmp_path)
    stale = storage.ensure_assets_dir(TOKEN) / "fig-09.jpg"
    stale.write_bytes(b"stale")

    llm = ScriptedLlm(PLAN_OUTPUT, SCAN_CLEAN, WRITE_OUTPUT)
    asyncio.run(_service(storage, llm, FakeFfmpeg()).generate(TOKEN, force=True))

    assert not stale.exists()
