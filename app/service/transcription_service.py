"""自建转写：抽音轨 -> 云端识别 -> 声纹认人 -> 拼成飞书同款转写。

只在飞书转写被截断时才走这条路（免费版妙记通常只转前几分钟）。云端文件识别对
单次音频长度有限制，实测二十几分钟就会被拒，所以按段提交；段内的 speaker_N 只在
段内有效，跨段的身份统一交给声纹层判定。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.core import runtime_config
from app.core.config import settings
from app.data_model.entity.voiceprint import (
    MeetingSpeakerUpsertEntity,
    VoiceprintCreateEntity,
    VoiceprintSampleCreateEntity,
    VoiceprintUpdateEntity,
)
from app.integrations.asr.step_asr_client import (
    AsrRequestError,
    AsrUtterance,
    StepAsrClient,
)
from app.integrations.video.ffmpeg_client import FfmpegError, ffmpeg_client
from app.repository.uow import UnitOfWork
from app.service import voiceprint_service as vp
from app.service.asr_transcript import TranscriptLine, build_transcript, local_label
from app.service.meeting_storage_service import MeetingStorageService
from app.service.transcript_anchor import parse_transcript_segments

logger = logging.getLogger(__name__)

# 同一个人连续说话，间隔小于该秒数算同一轮，用来找够长的声纹样本
TURN_GAP_SECONDS = 1.0
# 每位说话人最多留几段样本供试听
SAMPLES_PER_SPEAKER = 3
# 质心加权时单个人物的样本数上限，防止老条目重到再也更新不动
CENTROID_WEIGHT_CAP = 30
# 识别失败时对半重试的下限，再小就直接放弃这一小段
MIN_RETRY_SPLIT_SECONDS = 90.0
# 分段边界向前后各找这么久的静音，把刀口挪到没人说话的地方
BOUNDARY_SEARCH_SECONDS = 15.0
# 判定静音的音量门限与最短持续时间
SILENCE_NOISE_DB = -30.0
SILENCE_MIN_SECONDS = 0.4


class TranscriptionError(RuntimeError):
    """自建转写失败。"""


@dataclass
class _ChunkUtterance:
    text: str
    start_ms: int
    end_ms: int
    # 段号加段内说话人编号，才是全局唯一的候选人标识
    speaker_id: str
    chunk_index: int


@dataclass
class SpeakerOutcome:
    local_label: str
    voiceprint_id: int | None
    display_name: str | None
    match_score: float | None
    talk_ms: int
    segments: int
    is_new: bool
    # 实际算出声纹的样本数；为 0 说明这人没留下可用的样本，跨会议认不出来
    samples: int = 0


@dataclass
class TranscriptionResult:
    transcript: str
    utterances: int
    duration_seconds: float
    speakers: list[SpeakerOutcome] = field(default_factory=list)
    chunks: int = 0
    # 被内容审核卡住、切到最小粒度仍识别不了的时长
    skipped_seconds: float = 0.0


def transcript_coverage(transcript: str | None, duration_ms: int | None) -> float | None:
    """飞书转写覆盖了录音的多少：末条段落时间戳 / 总时长。

    返回 None 表示无从判断（没有时长或没有段落），交给调用方决定。
    """
    if not duration_ms or duration_ms <= 0:
        return None
    if not transcript:
        return 0.0
    segments = parse_transcript_segments(transcript)
    if not segments:
        return 0.0
    last_seconds = max(segment.start_seconds for segment in segments)
    return min(1.0, last_seconds * 1000.0 / float(duration_ms))


def needs_self_transcription(coverage: float | None) -> bool:
    if not runtime_config.get_bool("STEP_ASR_ENABLED", settings.step_asr_enabled):
        return False
    if coverage is None:
        return False
    threshold = runtime_config.get_float(
        "TRANSCRIPT_COVERAGE_THRESHOLD", settings.transcript_coverage_threshold
    )
    return coverage < threshold


@dataclass
class _Turn:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


def _recount_candidates(
    utterances: list[_ChunkUtterance], candidates: list[vp.SpeakerCandidate]
) -> None:
    """复核改判之后，发言时长与段数得按新的归属重算。"""
    by_cloud: dict[str, list[_ChunkUtterance]] = {}
    for item in utterances:
        by_cloud.setdefault(item.speaker_id, []).append(item)
    for candidate in candidates:
        items = [it for cid in candidate.cloud_ids for it in by_cloud.get(cid, [])]
        candidate.segments = len(items)
        candidate.talk_ms = sum(turn.duration_ms for turn in _build_turns(items))  # type: ignore[arg-type]


def _build_turns(utterances: list[AsrUtterance]) -> list[_Turn]:
    turns: list[_Turn] = []
    for item in sorted(utterances, key=lambda u: u.start_ms):
        if turns and item.start_ms - turns[-1].end_ms <= TURN_GAP_SECONDS * 1000:
            turns[-1].end_ms = max(turns[-1].end_ms, item.end_ms)
            continue
        turns.append(_Turn(start_ms=item.start_ms, end_ms=item.end_ms))
    return turns


class TranscriptionService:
    def __init__(
        self,
        storage: MeetingStorageService | None = None,
        asr_client: StepAsrClient | None = None,
    ) -> None:
        self._storage = storage or MeetingStorageService()
        self._asr = asr_client or StepAsrClient()

    async def transcribe(
        self,
        minute_token: str,
        *,
        owner_user_id: int,
        duration_ms: int | None = None,
        create_time: str | None = None,
        on_progress: Callable[[str, float], None] | None = None,
    ) -> TranscriptionResult:
        from app.service.r2_media_service import r2_media_service

        def progress(stage: str, percent: float) -> None:
            if on_progress is not None:
                on_progress(stage, percent)

        if not r2_media_service.enabled():
            raise TranscriptionError(
                "自建转写需要把音频放到公网直链上，当前未启用 R2 存储"
            )

        media = self._storage.find_media_path(
            minute_token, owner_user_id=owner_user_id
        )
        if media is None:
            raise TranscriptionError("本地没有该会议的音视频文件，无法转写")

        work_dir = self._storage.get_asr_work_dir(
            minute_token, owner_user_id=owner_user_id
        )
        audio_path = work_dir / "audio.mp3"

        progress("抽取音轨", 8.0)
        try:
            await ffmpeg_client.extract_audio(media, audio_path)
            info = await ffmpeg_client.probe(media)
        except FfmpegError as exc:
            raise TranscriptionError(f"抽取音轨失败：{exc}") from exc
        total_seconds = info.duration_seconds or (float(duration_ms or 0) / 1000.0)
        if total_seconds <= 0:
            raise TranscriptionError("无法确定录音时长，放弃自建转写")

        progress("上传音频", 14.0)
        audio_key = r2_media_service.asr_audio_key(
            minute_token, owner_user_id=owner_user_id
        )
        await r2_media_service.upload_asr_audio(
            audio_path, audio_key, minute_token=minute_token
        )

        utterances, skipped_seconds = await self._transcribe_chunks(
            audio_path,
            audio_key,
            total_seconds,
            minute_token=minute_token,
            owner_user_id=owner_user_id,
            progress=progress,
        )
        if not utterances:
            raise TranscriptionError("语音识别没有返回任何内容")

        progress("识别说话人", 70.0)
        candidates = await self._build_candidates(audio_path, utterances)

        progress("复核每句话的归属", 78.0)
        moved = await self._verify_utterances(audio_path, utterances, candidates)
        if moved:
            _recount_candidates(utterances, candidates)

        outcomes = await self._resolve_speakers(
            candidates, minute_token=minute_token, owner_user_id=owner_user_id
        )

        progress("整理转写文本", 92.0)
        label_by_cloud: dict[str, str] = {}
        for outcome, candidate in outcomes:
            for cloud_id in candidate.cloud_ids:
                label_by_cloud[cloud_id] = outcome.local_label
        lines = [
            TranscriptLine(
                speaker=label_by_cloud.get(item.speaker_id, local_label(1)),
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                text=item.text,
            )
            for item in utterances
        ]
        transcript = build_transcript(
            lines,
            duration_ms=int(total_seconds * 1000),
            create_time=create_time,
        )

        await asyncio.to_thread(
            self._storage.save_asr_transcript,
            minute_token,
            transcript,
            owner_user_id=owner_user_id,
        )
        await r2_media_service.sync_asr_transcript_text(
            minute_token, transcript, owner_user_id=owner_user_id
        )
        audio_path.unlink(missing_ok=True)

        progress("转写完成", 98.0)
        return TranscriptionResult(
            transcript=transcript,
            utterances=len(utterances),
            duration_seconds=total_seconds,
            speakers=[outcome for outcome, _ in outcomes],
            chunks=len({item.chunk_index for item in utterances}),
            skipped_seconds=skipped_seconds,
        )

    # ---------- 分段识别 ----------

    async def _chunk_bounds(
        self, audio_path: Path, total_seconds: float, chunk_seconds: float
    ) -> list[tuple[float, float]]:
        """算出每段的 (起点, 时长)，尽量让刀口落在静音处。

        按整数倍硬切会把一句话劈成两半：两边各得到一个残缺分句，接缝处的人还会
        因为分属两次调用而多出一个待认的候选。所以在算术边界附近找一段静音把刀口
        挪过去，找不到就留在原位，不影响流程。
        """
        count = int((total_seconds - 0.5) // chunk_seconds) + 1
        if count <= 1:
            return [(0.0, total_seconds)]

        raw = [index * chunk_seconds for index in range(1, count)]
        window = runtime_config.get_float(
            "ASR_BOUNDARY_SEARCH_SECONDS", BOUNDARY_SEARCH_SECONDS
        )
        # 窗口不能大到让相邻边界够得着彼此，否则挪完可能前后颠倒
        window = min(window, chunk_seconds / 4)
        if window <= 0:
            points = raw
        else:
            points = list(
                await asyncio.gather(
                    *(
                        self._snap_boundary(audio_path, at, window, total_seconds)
                        for at in raw
                    )
                )
            )

        edges = [0.0, *points, total_seconds]
        return [
            (edges[i], edges[i + 1] - edges[i])
            for i in range(len(edges) - 1)
            if edges[i + 1] - edges[i] > 0.5
        ]

    async def _snap_boundary(
        self, audio_path: Path, boundary: float, window: float, total_seconds: float
    ) -> float:
        """把一个分段边界挪到附近最近的静音中点。"""
        start = max(0.0, boundary - window)
        end = min(total_seconds, boundary + window)
        if end - start < 1.0:
            return boundary
        spans = await ffmpeg_client.detect_silence(
            audio_path,
            start,
            end - start,
            noise_db=SILENCE_NOISE_DB,
            min_silence_seconds=SILENCE_MIN_SECONDS,
        )
        best: float | None = None
        for span_start, span_end in spans:
            middle = (max(span_start, start) + min(span_end, end)) / 2
            if best is None or abs(middle - boundary) < abs(best - boundary):
                best = middle
        if best is None:
            logger.info("边界 %.0fs 附近没找到静音，按原位切开", boundary)
            return boundary
        logger.info("边界 %.0fs 挪到静音处 %.1fs", boundary, best)
        return best

    async def _transcribe_chunks(
        self,
        audio_path: Path,
        audio_key: str,
        total_seconds: float,
        *,
        minute_token: str,
        owner_user_id: int,
        progress: Callable[[str, float], None],
    ) -> tuple[list[_ChunkUtterance], float]:
        """分段送识别，返回全部分句与被跳过的秒数。"""
        from app.service.r2_media_service import r2_media_service

        chunk_seconds = max(
            60.0, runtime_config.get_float("ASR_CHUNK_MINUTES", 15.0) * 60.0
        )
        bounds = await self._chunk_bounds(audio_path, total_seconds, chunk_seconds)
        total_chunks = len(bounds)
        done = 0
        skipped = 0.0
        part_seq = 0
        lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(
            max(1, runtime_config.get_int("ASR_CHUNK_CONCURRENCY", 3))
        )

        async def call_asr(start: float, length: float) -> tuple[str, list[AsrUtterance]]:
            """切一段传上去识别，用完即删；异常原样抛给上层决定要不要再切。

            返回值带一个调用编号：每次调用的 speaker_N 只在这一次调用内有效，
            跨调用的身份得留给声纹层判定，编号必须彼此隔离。
            """
            nonlocal part_seq
            async with semaphore:
                part_seq += 1
                tag = f"c{part_seq}"
                if total_chunks == 1 and start <= 0 and length >= total_seconds - 1:
                    url = await r2_media_service.presign_asr_audio_key(audio_key)
                    chunk_key = None
                else:
                    part = audio_path.with_name(f"chunk_{part_seq:03d}.mp3")
                    await ffmpeg_client.extract_audio(
                        audio_path,
                        part,
                        start_seconds=start,
                        duration_seconds=length,
                    )
                    chunk_key = r2_media_service.asr_chunk_key(
                        minute_token, part_seq, owner_user_id=owner_user_id
                    )
                    await r2_media_service.upload_asr_audio(
                        part, chunk_key, minute_token=minute_token
                    )
                    part.unlink(missing_ok=True)
                    url = await r2_media_service.presign_asr_audio_key(chunk_key)
                try:
                    result = await self._asr.transcribe(url, audio_format="mp3")
                finally:
                    if chunk_key:
                        await r2_media_service.delete_asr_audio(chunk_key)
            return tag, result.utterances

        async def run_segment(
            index: int, start: float, length: float
        ) -> list[_ChunkUtterance]:
            """识别一段；失败先原样重来一次，还不行就对半切，最后才丢掉。

            识别服务偶尔会以「risk blocked」整段拒收，而且同一段音频这次拒下次
            又过，多半是内容审核在抖。所以先原样重试；真是内容问题才对半切，把
            损失从十几分钟压到一两分钟。切得越碎，云端说话人编号也越碎，能不切
            就不切。
            """
            nonlocal skipped
            tag = ""
            utterances: list[AsrUtterance] = []
            for attempt in (1, 2):
                try:
                    tag, utterances = await call_asr(start, length)
                    break
                except AsrRequestError as exc:
                    if attempt == 1:
                        logger.warning(
                            "识别失败，原样重试一次 token=%s %.0f~%.0fs：%s",
                            minute_token,
                            start,
                            start + length,
                            exc,
                        )
                        continue
                    if length <= MIN_RETRY_SPLIT_SECONDS:
                        skipped += length
                        logger.error(
                            "识别放弃一小段 token=%s %.0f~%.0fs（共 %.0fs）：%s",
                            minute_token,
                            start,
                            start + length,
                            length,
                            exc,
                        )
                        return []
                    logger.warning(
                        "识别连续失败，对半切开再试 token=%s %.0f~%.0fs：%s",
                        minute_token,
                        start,
                        start + length,
                        exc,
                    )
                    half = length / 2
                    head, tail = await asyncio.gather(
                        run_segment(index, start, half),
                        run_segment(index, start + half, length - half),
                    )
                    return head + tail

            offset_ms = int(start * 1000)
            return [
                _ChunkUtterance(
                    text=item.text,
                    start_ms=item.start_ms + offset_ms,
                    end_ms=item.end_ms + offset_ms,
                    # 段内编号加上调用编号才是全局唯一的候选人标识
                    speaker_id=f"{tag}:{item.speaker_id or 'na'}",
                    chunk_index=index,
                )
                for item in utterances
            ]

        async def run_chunk(index: int, start: float, length: float) -> list[_ChunkUtterance]:
            nonlocal done
            items = await run_segment(index, start, length)
            async with lock:
                done += 1
                progress(
                    f"云端识别 {done}/{total_chunks} 段",
                    16.0 + 56.0 * done / total_chunks,
                )
            return items

        progress(f"云端识别 0/{total_chunks} 段", 16.0)
        chunks = await asyncio.gather(
            *(
                run_chunk(index, start, length)
                for index, (start, length) in enumerate(bounds)
            )
        )
        merged = [item for chunk in chunks for item in chunk]
        merged.sort(key=lambda item: (item.start_ms, item.end_ms))
        logger.info(
            "自建转写识别完成 token=%s 段数=%d 分句=%d 放弃 %.0fs",
            minute_token,
            total_chunks,
            len(merged),
            skipped,
        )
        return merged, skipped

    # ---------- 声纹 ----------

    async def _build_candidates(
        self, audio_path: Path, utterances: list[_ChunkUtterance]
    ) -> list[vp.SpeakerCandidate]:
        """按云端说话人分组切样本算嵌入，再把会议内是同一个人的并起来。"""
        if not vp.extractor.available():
            logger.warning(
                "声纹模型不可用（未安装 sherpa-onnx 或模型文件缺失），"
                "本场只按云端分离结果编号，跨会议认人会失效"
            )

        grouped: dict[str, list[_ChunkUtterance]] = {}
        for item in utterances:
            grouped.setdefault(item.speaker_id, []).append(item)

        sample_count = max(
            1, runtime_config.get_int("SPEAKER_SAMPLE_COUNT", settings.speaker_sample_count)
        )
        sample_seconds = max(
            2.0,
            runtime_config.get_float(
                "SPEAKER_SAMPLE_SECONDS", settings.speaker_sample_seconds
            ),
        )
        min_turn_seconds = max(
            sample_seconds,
            runtime_config.get_float(
                "SPEAKER_MIN_SEGMENT_SECONDS", settings.speaker_min_segment_seconds
            ),
        )

        candidates: list[vp.SpeakerCandidate] = []
        for cloud_id, items in grouped.items():
            turns = _build_turns(items)  # type: ignore[arg-type]
            talk_ms = sum(turn.duration_ms for turn in turns)
            long_turns = [t for t in turns if t.duration_ms >= min_turn_seconds * 1000]
            if not long_turns:
                # 没有够长的整轮发言时退而求其次，取最长的几轮
                long_turns = sorted(turns, key=lambda t: t.duration_ms, reverse=True)[:2]
                long_turns = [t for t in long_turns if t.duration_ms >= 3000]
            long_turns.sort(key=lambda t: t.duration_ms, reverse=True)

            samples: list[vp.SpeakerSample] = []
            for turn in long_turns[: sample_count * 2]:
                if len(samples) >= sample_count:
                    break
                sample = await self._embed_turn(audio_path, turn, sample_seconds)
                if sample is not None:
                    samples.append(sample)

            candidate = vp.SpeakerCandidate(
                cloud_ids=[cloud_id],
                samples=samples,
                talk_ms=talk_ms,
                segments=len(items),
            )
            candidate.centroid = vp.trimmed_centroid(samples)
            candidates.append(candidate)

        return vp.cluster_candidates(candidates)

    async def _verify_utterances(
        self,
        audio_path: Path,
        utterances: list[_ChunkUtterance],
        candidates: list[vp.SpeakerCandidate],
    ) -> int:
        """逐句复核归属，把明显不像本组的句子改判给更像的那个人。

        云端的说话人分离是在单次调用内部做的，两个人声音接近时会把整句安错人，
        而声纹层只在「组」这一级取样，错的句子会一直跟着错的组走。这里给够长的
        句子单独算一次声纹，跟各人的质心比，明显更像别人才改——改判门槛留得高，
        宁可漏掉几句，也不要把本来对的搅乱。
        """
        if not runtime_config.get_bool("SPEAKER_VERIFY_ENABLED", True):
            return 0
        usable = [c for c in candidates if c.centroid]
        if len(usable) < 2 or not vp.extractor.available():
            return 0

        min_seconds = max(
            1.5,
            runtime_config.get_float(
                "SPEAKER_VERIFY_MIN_SECONDS", settings.speaker_verify_min_seconds
            ),
        )
        margin = runtime_config.get_float(
            "SPEAKER_VERIFY_MARGIN", settings.speaker_verify_margin
        )
        owner_of: dict[str, vp.SpeakerCandidate] = {
            cloud_id: candidate
            for candidate in candidates
            for cloud_id in candidate.cloud_ids
        }
        semaphore = asyncio.Semaphore(
            max(1, runtime_config.get_int("SPEAKER_VERIFY_CONCURRENCY", 4))
        )
        moved = 0
        lock = asyncio.Lock()

        async def check(index: int, item: _ChunkUtterance) -> None:
            nonlocal moved
            async with semaphore:
                turn = _Turn(start_ms=item.start_ms, end_ms=item.end_ms)
                sample = await self._embed_turn(
                    audio_path, turn, min_seconds, tag=f"_v{index}"
                )
            if sample is None:
                return
            current = owner_of.get(item.speaker_id)
            current_score = (
                vp.cosine(sample.embedding, current.centroid)
                if current and current.centroid
                else -1.0
            )
            best = max(
                usable, key=lambda c: vp.cosine(sample.embedding, c.centroid)
            )
            best_score = vp.cosine(sample.embedding, best.centroid)
            if best is current or best_score - current_score < margin:
                return
            async with lock:
                item.speaker_id = best.cloud_ids[0]
                moved += 1

        targets = [
            item
            for item in utterances
            if item.end_ms - item.start_ms >= min_seconds * 1000
        ]
        if not targets:
            return 0
        await asyncio.gather(
            *(check(index, item) for index, item in enumerate(targets))
        )
        logger.info(
            "逐句复核完成：复核 %d 句，改判 %d 句（共 %d 句）",
            len(targets),
            moved,
            len(utterances),
        )
        return moved

    async def _embed_turn(
        self, audio_path: Path, turn: _Turn, sample_seconds: float, tag: str = ""
    ) -> vp.SpeakerSample | None:
        """从一轮发言里切一段算声纹；开头一秒常是抢话或吸气，跳过。"""
        lead_in = 1.0 if turn.duration_ms >= (sample_seconds + 1.5) * 1000 else 0.0
        start = turn.start_ms / 1000.0 + lead_in
        length = min(sample_seconds, turn.duration_ms / 1000.0 - lead_in)
        if length < 2.0:
            return None
        # 并发切片时文件名必须互不相同，跨段偶尔会有起点相同的两句
        clip = audio_path.with_name(f"sample_{turn.start_ms}{tag}.wav")
        try:
            produced = await ffmpeg_client.extract_audio_clip(
                audio_path, start, length, clip
            )
            if produced is None:
                return None
            # onnx 推理是 CPU 密集的，别堵住事件循环
            embedding, energy = await asyncio.to_thread(vp.extractor.embed_file, clip)
        except (FfmpegError, vp.VoiceprintError) as exc:
            logger.warning("声纹样本处理失败 start=%.1fs: %s", start, exc)
            return None
        finally:
            clip.unlink(missing_ok=True)
        if not embedding:
            return None
        return vp.SpeakerSample(
            start_ms=int(start * 1000),
            end_ms=int((start + length) * 1000),
            embedding=embedding,
            rms=energy,
        )

    async def _resolve_speakers(
        self,
        candidates: list[vp.SpeakerCandidate],
        *,
        minute_token: str,
        owner_user_id: int,
    ) -> list[tuple[SpeakerOutcome, vp.SpeakerCandidate]]:
        """把会议内的说话人对到全局人物库，返回本地编号与人物的对应。"""
        threshold = vp.match_threshold()
        outcomes: list[tuple[SpeakerOutcome, vp.SpeakerCandidate]] = []

        async with UnitOfWork() as uow:
            assert uow.voiceprints is not None
            await uow.voiceprints.clear_speakers(
                minute_token, owner_user_id=owner_user_id
            )
            await uow.voiceprints.clear_samples_of_meeting(
                minute_token, owner_user_id=owner_user_id
            )
            library = await uow.voiceprints.list_active()
            known = [(item, vp.decode_embedding(item.embedding)) for item in library]
            claimed: dict[int, int] = {}  # voiceprint_id -> outcomes 下标

            for candidate in candidates:
                best_id: int | None = None
                best_score = 0.0
                if candidate.centroid:
                    for person, vector in known:
                        score = vp.cosine(candidate.centroid, vector)
                        if score >= threshold and score > best_score:
                            best_id, best_score = person.id, score

                if best_id is not None and best_id in claimed:
                    # 同一个人被云端拆成了两个候选，合并进同一个本地编号
                    index = claimed[best_id]
                    existing_outcome, existing_candidate = outcomes[index]
                    existing_candidate.cloud_ids.extend(candidate.cloud_ids)
                    existing_outcome.talk_ms += candidate.talk_ms
                    existing_outcome.segments += candidate.segments
                    continue

                is_new = best_id is None
                if best_id is None:
                    if not candidate.centroid:
                        # 没算出声纹（样本太短或模型缺失），只在本场内编号
                        outcomes.append(
                            (
                                SpeakerOutcome(
                                    local_label="",
                                    voiceprint_id=None,
                                    display_name=None,
                                    match_score=None,
                                    talk_ms=candidate.talk_ms,
                                    segments=candidate.segments,
                                    is_new=False,
                                    samples=0,
                                ),
                                candidate,
                            )
                        )
                        continue
                    created = await uow.voiceprints.create(
                        VoiceprintCreateEntity(
                            embedding=vp.encode_embedding(candidate.centroid),
                            dim=len(candidate.centroid),
                            sample_count=len(candidate.samples),
                            meeting_count=1,
                        )
                    )
                    best_id = created.id
                    known.append((created, list(candidate.centroid)))
                    display_name = None
                else:
                    person = next(p for p, _ in known if p.id == best_id)
                    weight = min(CENTROID_WEIGHT_CAP, max(1, person.sample_count))
                    merged = vp.weighted_merge(
                        vp.decode_embedding(person.embedding),
                        weight,
                        candidate.centroid,
                        len(candidate.samples) or 1,
                    )
                    await uow.voiceprints.update(
                        VoiceprintUpdateEntity(
                            id=best_id,
                            embedding=vp.encode_embedding(merged),
                            sample_count=person.sample_count + len(candidate.samples),
                            meeting_count=person.meeting_count + 1,
                        )
                    )
                    display_name = person.display_name
                    logger.info(
                        "声纹命中已有人物 token=%s vp=%s 相似度 %.3f 名字=%s",
                        minute_token,
                        best_id,
                        best_score,
                        display_name or "（未命名）",
                    )

                claimed[best_id] = len(outcomes)
                outcomes.append(
                    (
                        SpeakerOutcome(
                            local_label="",
                            voiceprint_id=best_id,
                            display_name=display_name,
                            match_score=best_score or None,
                            talk_ms=candidate.talk_ms,
                            segments=candidate.segments,
                            is_new=is_new,
                            samples=len(candidate.samples),
                        ),
                        candidate,
                    )
                )

            # 编号按首次发言时间排，读起来跟录音顺序一致
            def first_start(pair: tuple[SpeakerOutcome, vp.SpeakerCandidate]) -> int:
                samples = pair[1].samples
                return min((s.start_ms for s in samples), default=1 << 62)

            for index, pair in enumerate(sorted(outcomes, key=first_start), start=1):
                pair[0].local_label = local_label(index)

            for outcome, candidate in outcomes:
                await uow.voiceprints.upsert_speaker(
                    MeetingSpeakerUpsertEntity(
                        owner_user_id=owner_user_id,
                        minute_token=minute_token,
                        local_label=outcome.local_label,
                        cloud_ids=candidate.cloud_ids,
                        voiceprint_id=outcome.voiceprint_id,
                        match_score=outcome.match_score,
                        talk_ms=outcome.talk_ms,
                        segments=outcome.segments,
                    )
                )
                if outcome.voiceprint_id is None:
                    continue
                for sample in sorted(
                    candidate.samples, key=lambda s: s.rms, reverse=True
                )[:SAMPLES_PER_SPEAKER]:
                    await uow.voiceprints.add_sample(
                        VoiceprintSampleCreateEntity(
                            voiceprint_id=outcome.voiceprint_id,
                            owner_user_id=owner_user_id,
                            minute_token=minute_token,
                            start_ms=sample.start_ms,
                            end_ms=sample.end_ms,
                            score=vp.cosine(sample.embedding, candidate.centroid),
                        )
                    )
            await uow.commit()

        return outcomes


transcription_service = TranscriptionService()
