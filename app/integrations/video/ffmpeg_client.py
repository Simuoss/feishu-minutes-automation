"""ffmpeg / ffprobe 封装：探测视频信息与按时间点抽帧。

抽帧用输入端 seek（-ss 放在 -i 前），ffmpeg 会先跳到最近关键帧再解码，
对一小时的录屏也是百毫秒级，因此可以放心地为每个候选时间点抽一簇帧。
"""

import asyncio
import json
import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core import runtime_config
from app.core.config import settings

logger = logging.getLogger(__name__)

# 单帧抽取正常在 1 秒内完成，留足余量以防机械盘或超长文件
FRAME_TIMEOUT_SECONDS = 60.0
PROBE_TIMEOUT_SECONDS = 30.0
# 抽音轨要走完整个文件，几小时的录像也得留够时间
AUDIO_EXTRACT_TIMEOUT_SECONDS = 1800.0


# silencedetect 把结果打在 stderr 的 info 日志里，只能靠文本解析
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


class FfmpegError(RuntimeError):
    """ffmpeg / ffprobe 调用失败。"""


@dataclass
class VideoInfo:
    path: Path
    duration_seconds: float
    width: int
    height: int
    codec: str | None = None

    @property
    def has_video_stream(self) -> bool:
        return self.width > 0 and self.height > 0


async def _run(cmd: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        # Windows Proactor 上 kill 后只 wait() 可能永远等不到，管道也要继续排空
        logger.error(
            "ffmpeg/ffprobe 超时 %.0fs，准备强杀进程 cmd=%s；怀疑超大视频或磁盘忙",
            timeout,
            cmd[:3],
        )
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5.0)
        except TimeoutError:
            logger.error(
                "强杀后仍无法回收子进程 cmd=%s，协程将放弃等待以免纪要任务永久卡死",
                cmd[:3],
            )
            raise FfmpegError(
                f"命令执行超过 {timeout:.0f}s 被终止，且子进程回收失败: {cmd[0]}"
            ) from exc
        raise FfmpegError(
            f"命令执行超过 {timeout:.0f}s 被终止，怀疑文件损坏或磁盘极慢: {cmd[0]}"
        ) from exc
    return process.returncode or 0, stdout, stderr


def format_timestamp(seconds: float) -> str:
    """转成 ffmpeg 接受的 HH:MM:SS.mmm。"""
    total = max(0.0, seconds)
    hours, remainder = divmod(int(total), 3600)
    minutes, secs = divmod(remainder, 60)
    millis = round((total - int(total)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


class FfmpegClient:
    def __init__(self, ffmpeg_path: str | None = None, ffprobe_path: str | None = None) -> None:
        self._ffmpeg = ffmpeg_path or settings.ffmpeg_path
        self._ffprobe = ffprobe_path or settings.ffprobe_path

    async def probe(self, video_path: Path) -> VideoInfo:
        if not video_path.is_file():
            raise FfmpegError(f"待探测的视频文件不存在: {video_path}")

        cmd = [
            self._ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,codec_name",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(video_path),
        ]
        try:
            code, stdout, stderr = await _run(cmd, PROBE_TIMEOUT_SECONDS)
        except FileNotFoundError as exc:
            raise FfmpegError(
                f"找不到 ffprobe 可执行文件（当前配置 FFPROBE_PATH={self._ffprobe}），"
                "无法为视频会议生成配图纪要"
            ) from exc

        if code != 0:
            raise FfmpegError(
                f"ffprobe 探测失败 code={code}，怀疑文件不是有效视频或已损坏: "
                f"{stderr.decode('utf-8', errors='replace')[:300]}"
            )

        try:
            payload = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise FfmpegError("ffprobe 输出无法解析为 JSON，怀疑 ffprobe 版本不兼容") from exc

        streams = payload.get("streams") or []
        stream = streams[0] if streams else {}
        duration_raw = (payload.get("format") or {}).get("duration") or 0
        try:
            duration = float(duration_raw)
        except (TypeError, ValueError):
            duration = 0.0

        return VideoInfo(
            path=video_path,
            duration_seconds=duration,
            width=int(stream.get("width") or 0),
            height=int(stream.get("height") or 0),
            codec=stream.get("codec_name"),
        )

    async def extract_frame(
        self,
        video_path: Path,
        seconds: float,
        dest: Path,
        *,
        max_width: int | None = None,
        quality: int | None = None,
    ) -> Path | None:
        """抽取单帧，失败返回 None（越界或该处无法解码都属预期内情况）。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        width = max_width or runtime_config.get_int("FRAME_MAX_WIDTH", 1920)
        cmd = [
            self._ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-ss",
            format_timestamp(seconds),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            # 只在源比目标更宽时缩小，避免把低分辨率录屏放大糊掉
            "-vf",
            f"scale='min({width},iw)':-2",
            "-q:v",
            str(quality or runtime_config.get_int("FRAME_JPEG_QUALITY", 3)),
            str(dest),
        ]
        try:
            code, _stdout, stderr = await _run(cmd, FRAME_TIMEOUT_SECONDS)
        except FileNotFoundError as exc:
            raise FfmpegError(
                f"找不到 ffmpeg 可执行文件（当前配置 FFMPEG_PATH={self._ffmpeg}），"
                "无法为视频会议生成配图纪要"
            ) from exc

        if code != 0 or not dest.is_file() or dest.stat().st_size == 0:
            logger.warning(
                "抽帧未产出图片 t=%.1fs code=%s，怀疑该时间点越界或所在片段无法解码: %s",
                seconds,
                code,
                stderr.decode("utf-8", errors="replace")[:200],
            )
            dest.unlink(missing_ok=True)
            return None
        return dest

    async def extract_audio(
        self,
        media_path: Path,
        dest: Path,
        *,
        sample_rate: int = 16000,
        bitrate: str = "32k",
        start_seconds: float | None = None,
        duration_seconds: float | None = None,
        timeout: float | None = None,
    ) -> Path:
        """抽出单声道音轨供语音识别使用，可只取其中一段。

        统一转 16k 单声道：识别侧本来就按这个采样率建模，而且一小时只有十几兆，
        远低于文件识别接口 100MB 的上限。
        """
        if not media_path.is_file():
            raise FfmpegError(f"待抽取音轨的媒体文件不存在: {media_path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = [self._ffmpeg, "-y", "-nostdin", "-loglevel", "error"]
        if start_seconds is not None:
            cmd += ["-ss", format_timestamp(start_seconds)]
        if duration_seconds is not None:
            cmd += ["-t", f"{max(0.1, duration_seconds):.3f}"]
        cmd += [
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-b:a",
            bitrate,
            str(dest),
        ]
        try:
            code, _stdout, stderr = await _run(
                cmd, timeout or AUDIO_EXTRACT_TIMEOUT_SECONDS
            )
        except FileNotFoundError as exc:
            raise FfmpegError(
                f"找不到 ffmpeg 可执行文件（当前配置 FFMPEG_PATH={self._ffmpeg}），无法抽取音轨"
            ) from exc
        if code != 0 or not dest.is_file() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise FfmpegError(
                "抽取音轨失败 code="
                f"{code}，怀疑源文件没有音轨或已损坏: "
                f"{stderr.decode('utf-8', errors='replace')[:200]}"
            )
        return dest

    async def extract_audio_clip(
        self,
        media_path: Path,
        start_seconds: float,
        duration_seconds: float,
        dest: Path,
        *,
        sample_rate: int = 16000,
    ) -> Path | None:
        """切一段 16k 单声道 wav 供声纹使用；越界或无法解码时返回 None。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._ffmpeg,
            "-y",
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            format_timestamp(start_seconds),
            "-t",
            f"{max(0.1, duration_seconds):.3f}",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "wav",
            str(dest),
        ]
        try:
            code, _stdout, stderr = await _run(cmd, FRAME_TIMEOUT_SECONDS)
        except FileNotFoundError as exc:
            raise FfmpegError(
                f"找不到 ffmpeg 可执行文件（当前配置 FFMPEG_PATH={self._ffmpeg}），无法切分音频"
            ) from exc
        if code != 0 or not dest.is_file() or dest.stat().st_size == 0:
            logger.warning(
                "音频切片未产出 t=%.1fs code=%s，怀疑越界或该片段无法解码: %s",
                start_seconds,
                code,
                stderr.decode("utf-8", errors="replace")[:200],
            )
            dest.unlink(missing_ok=True)
            return None
        return dest

    async def detect_silence(
        self,
        media_path: Path,
        start_seconds: float,
        duration_seconds: float,
        *,
        noise_db: float = -30.0,
        min_silence_seconds: float = 0.4,
    ) -> list[tuple[float, float]]:
        """找出一段音频里的静音区间，返回绝对秒数的 (起, 止) 列表。

        只扫窗口内的这一小段，一次几十毫秒。找不到静音、或 ffmpeg 报错，
        都返回空列表交给调用方自己兜底，不往上抛。
        """
        if not media_path.is_file() or duration_seconds <= 0:
            return []
        cmd = [
            self._ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-nostats",
            # silencedetect 是 info 级日志，压到 error 就什么都看不到了
            "-loglevel",
            "info",
            "-ss",
            format_timestamp(start_seconds),
            "-t",
            f"{duration_seconds:.3f}",
            "-i",
            str(media_path),
            "-vn",
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_silence_seconds}",
            "-f",
            "null",
            "-",
        ]
        try:
            code, _stdout, stderr = await _run(cmd, FRAME_TIMEOUT_SECONDS)
        except (FileNotFoundError, FfmpegError) as exc:
            logger.warning("静音探测失败 t=%.1fs：%s", start_seconds, exc)
            return []
        if code != 0:
            logger.warning(
                "静音探测返回 code=%s t=%.1fs：%s",
                code,
                start_seconds,
                stderr.decode("utf-8", errors="replace")[:200],
            )
            return []

        # -ss 放在 -i 前，滤镜看到的时间从 0 开始，加回窗口起点才是绝对时间
        window_end = start_seconds + duration_seconds
        spans: list[tuple[float, float]] = []
        pending: float | None = None
        for line in stderr.decode("utf-8", errors="replace").splitlines():
            begin = _SILENCE_START_RE.search(line)
            if begin:
                pending = start_seconds + float(begin.group(1))
            finish = _SILENCE_END_RE.search(line)
            if finish and pending is not None:
                spans.append((pending, min(window_end, start_seconds + float(finish.group(1)))))
                pending = None
        # 静音一直延续到窗口末尾时只有 silence_start，没有配对的结束
        if pending is not None:
            spans.append((pending, window_end))
        return [(max(start_seconds, a), b) for a, b in spans if b > a]

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
        """围绕一个时间点抽多帧，返回 (实际秒数, 文件) 列表。"""
        targets: list[float] = []
        for offset in offsets:
            moment = base_seconds + offset
            if moment < 0:
                continue
            if duration_limit is not None and moment > duration_limit - 0.2:
                continue
            targets.append(moment)
        if not targets:
            targets = [max(0.0, base_seconds)]

        tasks = [
            self.extract_frame(
                video_path,
                moment,
                dest_dir / f"{prefix}_{index}.jpg",
            )
            for index, moment in enumerate(targets)
        ]
        results = await asyncio.gather(*tasks)
        return [(moment, path) for moment, path in zip(targets, results, strict=True) if path]

    async def compress_for_share(
        self,
        video_path: Path,
        dest: Path,
        *,
        max_height: int | None = None,
        crf: int | None = None,
        preset: str | None = None,
        timeout: float | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> Path:
        """在线程里跑 ffmpeg，避免占用事件循环；通过 -progress 回报百分比。"""
        if not video_path.is_file():
            raise FfmpegError(f"待压缩的视频不存在: {video_path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        height = (
            max_height
            if max_height is not None
            else runtime_config.get_int("R2_VIDEO_MAX_HEIGHT", 720)
        )
        quality = crf if crf is not None else runtime_config.get_int("R2_VIDEO_CRF", 32)
        speed = preset or runtime_config.get_str("R2_VIDEO_PRESET", "veryfast")
        limit = (
            timeout
            if timeout is not None
            else runtime_config.get_float("R2_VIDEO_COMPRESS_TIMEOUT_SECONDS", 21600.0)
        )
        duration = 0.0
        try:
            info = await self.probe(video_path)
            duration = max(0.0, info.duration_seconds)
        except FfmpegError:
            duration = 0.0
        cmd = [
            self._ffmpeg,
            "-y",
            "-nostats",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            str(video_path),
            "-vf",
            f"scale=-2:'min({height},ih)'",
            "-c:v",
            "libx264",
            "-preset",
            speed,
            "-crf",
            str(quality),
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-movflags",
            "+faststart",
            str(dest),
        ]
        try:
            code, stderr = await asyncio.to_thread(
                _compress_in_thread, cmd, dest, limit, duration, on_progress
            )
        except FileNotFoundError as exc:
            raise FfmpegError(
                f"找不到 ffmpeg 可执行文件（当前配置 FFMPEG_PATH={self._ffmpeg}），"
                "无法压缩分享视频"
            ) from exc
        if code != 0 or not dest.is_file() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise FfmpegError(
                "分享视频压缩失败，怀疑源文件损坏、无视频流或磁盘空间不足: "
                f"{stderr[:400]}"
            )
        return dest


def parse_ffmpeg_out_time_ms(line: str) -> int | None:
    text = line.strip()
    if not text.startswith("out_time_ms="):
        return None
    raw = text.split("=", 1)[1].strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _compress_in_thread(
    cmd: list[str],
    dest: Path,
    timeout: float,
    duration: float,
    on_progress: Callable[[float], None] | None,
) -> tuple[int, str]:
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        raise
    last_report = -1.0
    try:
        assert process.stdout is not None
        for line in process.stdout:
            ms = parse_ffmpeg_out_time_ms(line)
            if ms is None or duration <= 0 or on_progress is None:
                continue
            percent = min(99.0, (ms / 1_000_000.0) / duration * 100.0)
            if percent >= last_report + 1:
                last_report = percent
                on_progress(percent)
        process.wait(timeout=max(1.0, timeout))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        dest.unlink(missing_ok=True)
        raise FfmpegError(f"分享视频压缩超过 {timeout:.0f}s 被终止") from None
    stderr = ""
    if process.stderr is not None:
        stderr = process.stderr.read()
    return process.returncode or 0, stderr


ffmpeg_client = FfmpegClient()
