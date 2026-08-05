"""ffmpeg / ffprobe 封装：探测视频信息与按时间点抽帧。

抽帧用输入端 seek（-ss 放在 -i 前），ffmpeg 会先跳到最近关键帧再解码，
对一小时的录屏也是百毫秒级，因此可以放心地为每个候选时间点抽一簇帧。
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

# 单帧抽取正常在 1 秒内完成，留足余量以防机械盘或超长文件
FRAME_TIMEOUT_SECONDS = 60.0
PROBE_TIMEOUT_SECONDS = 30.0


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
        width = max_width or settings.frame_max_width
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
            str(quality or settings.frame_jpeg_quality),
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
    ) -> Path:
        """重度压缩为分享用 mp4（原片保留，输出另存）。"""
        if not video_path.is_file():
            raise FfmpegError(f"待压缩的视频不存在: {video_path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        height = max_height if max_height is not None else settings.r2_video_max_height
        quality = crf if crf is not None else settings.r2_video_crf
        speed = preset or settings.r2_video_preset
        limit = timeout if timeout is not None else settings.r2_video_compress_timeout_seconds
        cmd = [
            self._ffmpeg,
            "-y",
            "-loglevel",
            "error",
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
            code, _stdout, stderr = await _run(cmd, limit)
        except FileNotFoundError as exc:
            raise FfmpegError(
                f"找不到 ffmpeg 可执行文件（当前配置 FFMPEG_PATH={self._ffmpeg}），"
                "无法压缩分享视频"
            ) from exc
        if code != 0 or not dest.is_file() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise FfmpegError(
                "分享视频压缩失败，怀疑源文件损坏、无视频流或磁盘空间不足: "
                f"{stderr.decode('utf-8', errors='replace')[:400]}"
            )
        return dest


ffmpeg_client = FfmpegClient()
