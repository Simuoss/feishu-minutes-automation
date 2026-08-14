"""配图纪要的前两步：让模型挑出想看的时刻，再把这些时刻抽成可用的截图。

真正写文档前会先做敏感信息脱敏；成文调用再同时拿到转写和（已打码的）图片。
这里不做图注生成，只负责"把该看的画面准备好"。
"""

import json
import logging
import math
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core import runtime_config
from app.integrations.llm.messages_client import LlmClient, LlmImage
from app.integrations.video.ffmpeg_client import FfmpegClient, VideoInfo
from app.service.frame_selection import (
    FrameStats,
    analyze_frame,
    select_distinct_frames,
)
from app.service.summary_scene import Scene, load_scene_prompt
from app.service.transcript_anchor import format_seconds, parse_seconds

logger = logging.getLogger(__name__)

PLAN_PROMPT_BASE = "screenshot_plan"
ADDENDUM_PROMPT_BASE = "illustrated_addendum"

PLAN_MAX_TOKENS = 8192

# 一簇帧全是空白/过渡时，用更大的偏移再试一次（纯算法重抽，不花模型调用）
FALLBACK_OFFSETS = [15.0, 30.0, -10.0, 45.0]

# 规划前均匀抽几帧，交给模型判断是否共享屏幕（不做算法门槛）。
PLAN_SAMPLE_COUNT = 8

IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)")


def figure_limit_for_duration(duration_seconds: float) -> int:
    """按时长计算本场最多配几张图。

    规则：一小时以内最多 first_hour 张；之后每多半小时多 per 张，
    不足半小时按半小时算（向上取整）。
    """
    base = max(1, runtime_config.get_int("SUMMARY_FIGURES_FIRST_HOUR", 20))
    per = max(0, runtime_config.get_int("SUMMARY_FIGURES_PER_EXTRA_HALF_HOUR", 6))
    if duration_seconds <= 0:
        return base
    if duration_seconds <= 3600:
        return base
    extra_half_hours = math.ceil((duration_seconds - 3600) / 1800)
    return base + extra_half_hours * per


@dataclass
class ScreenshotPlanItem:
    timestamp: str
    seconds: int
    expect: str
    purpose: str
    priority: int


@dataclass
class ScreenshotPlan:
    """模型对"这节课有没有共享屏幕"的判断，以及据此给出的截图候选。"""

    screen_shared: bool
    reason: str
    items: list[ScreenshotPlanItem]

    @property
    def usable(self) -> bool:
        return self.screen_shared and bool(self.items)


@dataclass
class PreparedFigure:
    figure_id: str
    relative_path: str
    path: Path
    timestamp: str
    frame_seconds: float
    expect: str
    purpose: str


def extract_json_object(text: str) -> dict[str, object] | None:
    """从模型输出里取出 JSON 对象，容忍代码块包裹和前后多余文字。"""
    stripped = text.strip()
    if not stripped:
        return None

    fenced = re.search(r"```(?:json)?\s*(.+?)```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        logger.error(
            "模型输出中找不到 JSON 对象，怀疑它改用自然语言作答或提示词被忽略。原始内容: %s",
            text[:300],
        )
        return None

    payload = stripped[start : end + 1]
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        # 不是致命错误：上层会尝试从残缺内容里抢救已完整的候选项
        logger.warning(
            "截图计划 JSON 不完整（%s），怀疑模型输出被截断或流式响应中断，"
            "将尝试抢救其中完整的候选项。原始内容: %s",
            exc,
            payload[:300],
        )
        return None

    if not isinstance(parsed, dict):
        logger.error("模型返回的 JSON 顶层不是对象，无法按约定字段读取。实际类型: %s", type(parsed))
        return None
    return parsed


def salvage_figure_objects(text: str) -> list[dict[str, object]]:
    """从被截断的计划里捞出已经完整的候选项。

    模型服务不稳时流会在中途断掉，此时整份 JSON 不合法，但前面若干条候选往往
    是完整的。整份丢弃等于白跑一次调用，因此按花括号配对逐条抢救。
    """
    salvaged: list[dict[str, object]] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
            # 根对象是 depth 1，figures 数组里的每个候选是 depth 2
            if depth == 2:
                start = index
        elif char == "}":
            if depth == 2 and start != -1:
                try:
                    entry = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    entry = None
                if isinstance(entry, dict):
                    salvaged.append(entry)
                start = -1
            depth -= 1

    return salvaged


def _read_screen_shared(payload: dict[str, object], *, has_figures: bool) -> tuple[bool, str]:
    """读模型对"有没有共享屏幕"的判断。

    字段缺失时不能一律当作 false——那会让老模型或偶尔漏写字段的响应彻底失去配图能力，
    因此退回到"它是否挑出了候选"这个行为信号上。
    """
    raw_flag = payload.get("screen_shared")
    reason = str(payload.get("reason") or "").strip()
    if isinstance(raw_flag, bool):
        return raw_flag, reason
    if isinstance(raw_flag, str) and raw_flag.strip().lower() in {"true", "false"}:
        return raw_flag.strip().lower() == "true", reason

    logger.warning(
        "截图计划里没有 screen_shared 字段，怀疑模型忽略了提示词要求，"
        "退回按它是否挑出候选来判断（本次候选数 %s）",
        "有" if has_figures else "无",
    )
    return has_figures, reason


def parse_plan(raw: str, *, limit: int) -> ScreenshotPlan:
    """解析截图计划：先取屏幕共享判断，再取候选项。"""
    payload = extract_json_object(raw)
    figures: object
    if payload is None:
        salvaged = salvage_figure_objects(raw)
        if not salvaged:
            return ScreenshotPlan(screen_shared=False, reason="", items=[])
        logger.warning(
            "截图计划 JSON 不完整，已抢救出 %d 条完整候选继续配图，"
            "怀疑模型输出被截断或流式响应中断",
            len(salvaged),
        )
        # 截断的输出读不到判断字段，但它既然已经在列候选，说明判断结果是"有共享"
        recovered: dict[str, object] = {"screen_shared": True, "figures": salvaged}
        payload = recovered
        figures = salvaged
    else:
        figures = payload.get("figures")

    if not isinstance(figures, list):
        logger.error("截图计划缺少 figures 数组，怀疑模型未按约定格式输出，本次不配图")
        return ScreenshotPlan(screen_shared=False, reason="", items=[])

    screen_shared, reason = _read_screen_shared(payload, has_figures=bool(figures))
    if not screen_shared:
        if figures:
            logger.warning(
                "模型判定本场没有共享屏幕却仍给出了 %d 个候选，以判定结果为准全部丢弃。理由：%s",
                len(figures),
                reason or "（未说明）",
            )
        return ScreenshotPlan(screen_shared=False, reason=reason, items=[])

    items: list[ScreenshotPlanItem] = []
    for entry in figures:
        if not isinstance(entry, dict):
            continue
        timestamp = str(entry.get("timestamp") or "").strip()
        seconds = parse_seconds(timestamp)
        if seconds is None:
            logger.warning(
                "截图计划中的时间点无法解析已跳过: %r，怀疑模型未按 HH:MM:SS 输出",
                timestamp,
            )
            continue
        raw_priority = entry.get("priority")
        # 模型偶尔把优先级写成字符串，或写成"高/中/低"这类无法比较的词
        if isinstance(raw_priority, int):
            priority = raw_priority
        elif isinstance(raw_priority, str) and raw_priority.strip().isdigit():
            priority = int(raw_priority)
        else:
            priority = 2
        items.append(
            ScreenshotPlanItem(
                timestamp=format_seconds(seconds),
                seconds=seconds,
                expect=str(entry.get("expect") or "").strip(),
                purpose=str(entry.get("purpose") or "").strip(),
                priority=max(1, min(3, priority)),
            )
        )

    items.sort(key=lambda i: i.seconds)
    if len(items) > limit:
        # 超额时按优先级裁剪，同级保持时间顺序
        items = sorted(items, key=lambda i: (i.priority, i.seconds))[:limit]
        items.sort(key=lambda i: i.seconds)
        logger.info("截图计划超过上限 %d，已按优先级裁剪", limit)

    return ScreenshotPlan(screen_shared=True, reason=reason, items=items)


def _candidate_reporter(on_candidate: Callable[[str, str], None]) -> Callable[[str], None]:
    """把流式 JSON 里刚写完的候选项逐条报出去。

    模型是一条条往外写的，每写完一个 `{...}` 就能确定一处画面，不必等整份 JSON 收完。
    """
    buffer: list[str] = []
    reported = 0

    def on_delta(chunk: str) -> None:
        nonlocal reported
        buffer.append(chunk)
        # 没有右花括号说明没有新的完整候选，不必重扫
        if "}" not in chunk:
            return
        entries = salvage_figure_objects("".join(buffer))
        for entry in entries[reported:]:
            timestamp = str(entry.get("timestamp") or "").strip()
            if parse_seconds(timestamp) is None:
                continue
            on_candidate(timestamp, str(entry.get("expect") or "").strip())
        reported = len(entries)

    return on_delta


class SummaryIllustrationService:
    def __init__(
        self,
        llm: LlmClient,
        ffmpeg: FfmpegClient,
    ) -> None:
        self._llm = llm
        self._ffmpeg = ffmpeg

    @staticmethod
    def load_addendum(scene: Scene) -> str:
        return load_scene_prompt(ADDENDUM_PROMPT_BASE, scene)

    async def probe_video(self, video_path: Path) -> VideoInfo:
        return await self._ffmpeg.probe(video_path)

    async def extract_plan_samples(self, video: VideoInfo, work_dir: Path) -> list[Path]:
        """均匀抽若干帧，仅供模型判断是否共享屏幕并规划配图时刻。"""
        duration = video.duration_seconds
        if duration <= 0:
            logger.warning(
                "视频时长探测为 0，无法均匀抽帧，怀疑容器缺少时长信息；本次不配图"
            )
            return []

        # 掐掉首尾各 5%，开头常是黑屏、结尾常是散会画面
        span_start = duration * 0.05
        span = duration * 0.9
        step = span / max(1, PLAN_SAMPLE_COUNT - 1)
        moments = [span_start + step * i for i in range(PLAN_SAMPLE_COUNT)]

        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        burst = await self._ffmpeg.extract_burst(
            video.path,
            0.0,
            moments,
            work_dir,
            "plan",
            duration_limit=duration,
        )
        sample_paths = [path for _, path in burst if path.is_file()]
        if not sample_paths:
            logger.warning(
                "均匀抽帧未产出可读样本，怀疑 ffmpeg 抽帧失败或文件已被清理；本次不配图"
            )
            shutil.rmtree(work_dir, ignore_errors=True)
            return []

        logger.info(
            "已均匀抽取 %d 帧样本交给模型判断是否共享屏幕（视频约 %.0f 分钟）",
            len(sample_paths),
            duration / 60,
        )
        return sample_paths

    async def plan_screenshots(
        self,
        transcript: str,
        *,
        sample_frames: list[Path],
        limit: int,
        scene: Scene,
        on_attempt: Callable[[int, int], None] | None = None,
        on_candidate: Callable[[str, str], None] | None = None,
    ) -> ScreenshotPlan:
        """模型先看抽帧样本判断是否共享屏幕，再据转写挑出必须看画面的时刻。

        screen_shared 只允许依据图片；转写只用于挑选 timestamp。
        """
        images: list[LlmImage] = []
        for path in sample_frames:
            if not path.is_file():
                continue
            try:
                images.append(LlmImage(data=path.read_bytes()))
            except OSError as exc:
                logger.warning(
                    "读取开场样本帧失败 path=%s，怀疑抽帧文件已被清理: %s",
                    path,
                    exc,
                )

        if not images:
            logger.error(
                "没有可读的抽帧样本却进入了截图规划，怀疑开场探测与规划之间文件被删；本次不配图"
            )
            return ScreenshotPlan(
                screen_shared=False,
                reason="无可用抽帧样本，无法做视觉判断",
                items=[],
            )

        prompt = load_scene_prompt(PLAN_PROMPT_BASE, scene)
        user_content = (
            f"下面附上 {len(images)} 张从录像中均匀抽取的画面样本。"
            f"请**只根据这些画面**判断 screen_shared；转写仅用于挑选配图时间点。\n"
            f"候选上限：{limit} 条。\n\n"
            f"<transcript>\n{transcript}\n</transcript>"
        )
        completion = await self._llm.complete(
            prompt,
            user_content,
            images=images,
            max_tokens=PLAN_MAX_TOKENS,
            on_attempt=on_attempt,
            on_delta=_candidate_reporter(on_candidate) if on_candidate else None,
        )
        plan = parse_plan(completion.text, limit=limit)
        if not plan.screen_shared:
            logger.info(
                "模型依据抽帧画面判定本场没有共享屏幕，不配图。理由：%s",
                plan.reason or "（未说明）",
            )
        else:
            logger.info(
                "截图计划已生成：%d 个时间点（模型输出 %d 字 / %d tokens）。视觉判定：%s",
                len(plan.items),
                len(completion.text),
                completion.output_tokens,
                plan.reason or "（未说明）",
            )
        return plan

    async def _grab_group(
        self,
        video: VideoInfo,
        item: ScreenshotPlanItem,
        work_dir: Path,
        index: int,
    ) -> list[FrameStats]:
        offsets = runtime_config.frame_burst_offset_list()
        burst = await self._ffmpeg.extract_burst(
            video.path,
            float(item.seconds),
            offsets,
            work_dir,
            f"cand{index:02d}",
            duration_limit=video.duration_seconds,
        )
        stats = [s for s in (analyze_frame(p, sec) for sec, p in burst) if s is not None]

        if any(not s.is_blank for s in stats):
            return stats

        # 全是黑屏/过渡帧：放宽偏移再抽一次，仍是纯算法，不需要模型介入
        logger.info(
            "%s 附近抽到的 %d 帧全是空白或过渡画面，放宽偏移重抽一次",
            item.timestamp,
            len(stats),
        )
        retry = await self._ffmpeg.extract_burst(
            video.path,
            float(item.seconds),
            FALLBACK_OFFSETS[: runtime_config.get_int("FRAME_REGRAB_LIMIT", 3)],
            work_dir,
            f"cand{index:02d}r",
            duration_limit=video.duration_seconds,
        )
        stats.extend(s for s in (analyze_frame(p, sec) for sec, p in retry) if s is not None)
        return stats

    async def prepare_figures(
        self,
        minute_token: str,
        video: VideoInfo,
        plan: list[ScreenshotPlanItem],
        assets_dir: Path,
        work_dir: Path,
        *,
        limit: int | None = None,
    ) -> list[PreparedFigure]:
        """第二步：抽帧、去空白、去重，落盘成 fig-NN.jpg。

        候选帧写在 work_dir（不对外暴露的中间目录），只有最终选中的图才进 assets_dir。
        """
        if not plan:
            return []

        figure_limit = limit if limit is not None else figure_limit_for_duration(
            video.duration_seconds
        )
        assets_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        groups: list[list[FrameStats]] = []
        kept_plan: list[ScreenshotPlanItem] = []
        for index, item in enumerate(plan):
            stats = await self._grab_group(video, item, work_dir, index)
            if stats:
                groups.append(stats)
                kept_plan.append(item)

        # select_distinct_frames 按组顺序挑选，返回的每一项都能对回原计划
        chosen: list[PreparedFigure] = []
        picked = select_distinct_frames(groups, limit=figure_limit)
        picked_paths = {stats.path: stats for stats in picked}

        figure_index = 0
        for item, group in zip(kept_plan, groups, strict=True):
            match = next((s for s in group if s.path in picked_paths), None)
            if match is None:
                continue
            figure_index += 1
            figure_id = f"fig-{figure_index:02d}"
            dest = assets_dir / f"{figure_id}.jpg"
            dest.unlink(missing_ok=True)
            match.path.replace(dest)
            chosen.append(
                PreparedFigure(
                    figure_id=figure_id,
                    relative_path=f"assets/{figure_id}.jpg",
                    path=dest,
                    timestamp=item.timestamp,
                    frame_seconds=match.seconds,
                    expect=item.expect,
                    purpose=item.purpose,
                )
            )

        # 候选帧不再需要，留着只会占几十兆磁盘
        shutil.rmtree(work_dir, ignore_errors=True)

        logger.info(
            "配图准备完成 token=%s：计划 %d 个时间点，实际产出 %d 张可用截图",
            minute_token,
            len(plan),
            len(chosen),
        )
        return chosen

    @staticmethod
    def build_manifest(figures: list[PreparedFigure]) -> str:
        """给写作那次调用看的图片清单，顺序与图片在请求中的顺序一致。"""
        lines = ["<figures>"]
        for fig in figures:
            lines.append(f"- {fig.figure_id} | 路径 `{fig.relative_path}` | 时间点 `{fig.timestamp}`")
            lines.append(f"  当初想看：{fig.expect or '（未指定）'}")
            lines.append(f"  预期用途：{fig.purpose or '（未指定）'}")
        lines.append("</figures>")
        return "\n".join(lines)

    @staticmethod
    def to_images(figures: list[PreparedFigure]) -> list[LlmImage]:
        return [LlmImage(data=fig.path.read_bytes()) for fig in figures]

    @staticmethod
    def referenced_ids(markdown: str, figures: list[PreparedFigure]) -> set[str]:
        refs = {match.strip() for match in IMAGE_REF_RE.findall(markdown)}
        used: set[str] = set()
        for fig in figures:
            if fig.relative_path in refs or fig.path.name in refs:
                used.add(fig.figure_id)
        return used

    @classmethod
    def drop_unreferenced(
        cls,
        markdown: str,
        figures: list[PreparedFigure],
    ) -> list[PreparedFigure]:
        """删掉模型没用上的图。

        未被引用的图从磁盘删除，避免无用截图随文档留存。
        """
        used = cls.referenced_ids(markdown, figures)
        kept: list[PreparedFigure] = []
        for fig in figures:
            if fig.figure_id in used:
                kept.append(fig)
                continue
            fig.path.unlink(missing_ok=True)
        if len(kept) != len(figures):
            logger.info(
                "模型未引用 %d 张截图，已从磁盘删除",
                len(figures) - len(kept),
            )
        return kept
