"""从飞书转写的真名自动提炼声纹，产出待确认的命名提案。

飞书转写里写的是真名，这是难得的免费标注；缺的只是「这个名字对应什么声音」。
把每位有名字的发言人的音频切出来算成质心，再去全局人物库里比一比，就能提出
「库里 #12 那位其实叫某某」或者「这是个新面孔」。

姓名可信，但飞书的发言人切分会串行——同一段里混进别人的话是常事，所以结果一律
先落成 PENDING 提案，等人试听确认后才真动人物库。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core import runtime_config
from app.core.config import settings
from app.data_model.entity.voiceprint import (
    PROPOSAL_APPROVED,
    PROPOSAL_PENDING,
    PROPOSAL_REJECTED,
    ProposalSample,
    VoiceprintCreateEntity,
    VoiceprintNameProposalCreateEntity,
    VoiceprintNameProposalEntity,
    VoiceprintSampleCreateEntity,
    VoiceprintUpdateEntity,
)
from app.integrations.video.ffmpeg_client import FfmpegError, ffmpeg_client
from app.repository.uow import UnitOfWork
from app.service import voiceprint_service as vp
from app.service.audio_source_service import (
    AudioSourceError,
    cleanup_refetched_media,
    ensure_asr_audio,
)
from app.service.meeting_storage_service import MeetingStorageService
from app.service.transcript_anchor import SEGMENT_HEAD_RE

logger = logging.getLogger(__name__)

# 「说话人1」「发言人 2」「Speaker 3」这类占位名不是真名，认了也没意义
PLACEHOLDER_NAME_RE = re.compile(
    r"^(说话人|发言人|讲话人|speaker|spk)\s*\d*$", re.IGNORECASE
)
# 末段没有下一段起点可用时，假定它说了这么久
TAIL_SEGMENT_SECONDS = 30.0
# 质心加权时单个人物的样本数上限，与自建转写那边保持一致
CENTROID_WEIGHT_CAP = 30

_storage = MeetingStorageService()


class VoiceprintHarvestError(RuntimeError):
    """声纹提炼失败。"""


@dataclass
class _NamedSpeaker:
    name: str
    spans: list[tuple[float, float]] = field(default_factory=list)
    samples: list[vp.SpeakerSample] = field(default_factory=list)
    centroid: list[float] = field(default_factory=list)


@dataclass
class HarvestResult:
    proposals: int
    skipped_named: int
    speakers_examined: int
    message: str


def is_placeholder_name(name: str) -> bool:
    cleaned = (name or "").strip()
    if not cleaned:
        return True
    return bool(PLACEHOLDER_NAME_RE.match(cleaned))


def parse_named_spans(
    transcript: str, *, duration_ms: int | None = None
) -> dict[str, list[tuple[float, float]]]:
    """把飞书原文解析成「真名 -> 发言区间」。

    段头只有起始时间，结束时间取下一段的起点；最后一段没有下一段，按固定时长估。
    """
    heads = list(SEGMENT_HEAD_RE.finditer(transcript))
    total_seconds = (duration_ms or 0) / 1000.0
    by_name: dict[str, list[tuple[float, float]]] = {}
    for index, head in enumerate(heads):
        name = head.group(1).strip()
        if is_placeholder_name(name):
            continue
        start = (
            int(head.group(2)) * 3600
            + int(head.group(3)) * 60
            + int(head.group(4))
        )
        if index + 1 < len(heads):
            nxt = heads[index + 1]
            end = (
                int(nxt.group(2)) * 3600
                + int(nxt.group(3)) * 60
                + int(nxt.group(4))
            )
        elif total_seconds > start:
            end = total_seconds
        else:
            end = start + TAIL_SEGMENT_SECONDS
        if end > start:
            by_name.setdefault(name, []).append((float(start), float(end)))
    return by_name


async def harvest_meeting(
    minute_token: str,
    *,
    owner_user_id: int,
    duration_ms: int | None = None,
    on_progress=None,
) -> HarvestResult:
    """对一场会议做声纹提炼，写入 PENDING 提案。"""

    def progress(stage: str, percent: float) -> None:
        if on_progress is not None:
            on_progress(stage, percent)

    if not vp.extractor.available():
        raise VoiceprintHarvestError(
            "声纹模型不可用：请确认已安装 sherpa-onnx 且模型文件已下载"
        )

    transcript = await asyncio.to_thread(
        _storage.read_feishu_transcript,
        minute_token,
        owner_user_id=owner_user_id,
    )
    if not transcript:
        raise VoiceprintHarvestError("本地没有飞书原文转写，无法按姓名提炼声纹")

    progress("解析飞书转写", 5.0)
    min_seconds = runtime_config.get_float(
        "SPEAKER_MIN_SEGMENT_SECONDS", settings.speaker_min_segment_seconds
    )
    sample_seconds = runtime_config.get_float(
        "SPEAKER_SAMPLE_SECONDS", settings.speaker_sample_seconds
    )
    sample_count = runtime_config.get_int(
        "SPEAKER_SAMPLE_COUNT", settings.speaker_sample_count
    )

    named = parse_named_spans(transcript, duration_ms=duration_ms)
    speakers: list[_NamedSpeaker] = []
    for name, spans in named.items():
        usable = [s for s in spans if s[1] - s[0] >= min_seconds]
        if not usable:
            continue
        usable.sort(key=lambda s: s[1] - s[0], reverse=True)
        speakers.append(_NamedSpeaker(name=name, spans=usable[: sample_count * 2]))
    if not speakers:
        return HarvestResult(
            proposals=0,
            skipped_named=0,
            speakers_examined=0,
            message=(
                "飞书转写里没有够长的实名发言段落"
                f"（需要至少 {min_seconds:.0f} 秒），没有可提炼的声纹"
            ),
        )

    progress("准备音轨", 15.0)
    try:
        audio = await ensure_asr_audio(
            minute_token, owner_user_id=owner_user_id
        )
    except AudioSourceError as exc:
        raise VoiceprintHarvestError(str(exc)) from exc

    try:
        progress("切片算声纹", 30.0)
        for index, speaker in enumerate(speakers):
            speaker.samples = await _embed_spans(
                audio.local_path,
                speaker.spans,
                sample_seconds=sample_seconds,
                want=sample_count,
                tag=str(index),
            )
            speaker.centroid = vp.trimmed_centroid(speaker.samples)
            progress(
                f"切片算声纹（{index + 1}/{len(speakers)}）",
                30.0 + 50.0 * (index + 1) / len(speakers),
            )
    finally:
        cleanup_refetched_media(
            audio, minute_token, owner_user_id=owner_user_id
        )

    usable_speakers = [s for s in speakers if s.centroid]
    progress("与人物库比对", 85.0)
    proposals, skipped = await _propose(
        usable_speakers, minute_token=minute_token, owner_user_id=owner_user_id
    )
    progress("提炼完成", 100.0)

    logger.info(
        "声纹提炼完成 token=%s 实名说话人=%d 可用=%d 新提案=%d 已同名跳过=%d",
        minute_token,
        len(speakers),
        len(usable_speakers),
        proposals,
        skipped,
    )
    if proposals == 0 and skipped == 0:
        message = "没能从这场会议里算出可用的声纹，多半是发言片段太吵或太短"
    elif proposals == 0:
        message = f"{skipped} 位说话人已在库中且名字一致，没有需要确认的提案"
    else:
        message = f"生成 {proposals} 条待确认提案"
    return HarvestResult(
        proposals=proposals,
        skipped_named=skipped,
        speakers_examined=len(speakers),
        message=message,
    )


async def _embed_spans(
    audio_path: Path,
    spans: list[tuple[float, float]],
    *,
    sample_seconds: float,
    want: int,
    tag: str,
) -> list[vp.SpeakerSample]:
    """从若干发言区间里切出样本算声纹，够数就停。"""
    samples: list[vp.SpeakerSample] = []
    for index, (start, end) in enumerate(spans):
        if len(samples) >= want:
            break
        # 段首一秒常是抢话或吸气，往后挪一点再取
        lead_in = 1.0 if end - start >= sample_seconds + 1.5 else 0.0
        length = min(sample_seconds, end - start - lead_in)
        if length < 2.0:
            continue
        clip = audio_path.with_name(f"harvest_{tag}_{index}.wav")
        try:
            produced = await ffmpeg_client.extract_audio_clip(
                audio_path, start + lead_in, length, clip
            )
            if produced is None:
                continue
            embedding, energy = await asyncio.to_thread(
                vp.extractor.embed_file, clip
            )
        except (FfmpegError, vp.VoiceprintError) as exc:
            logger.warning("声纹样本处理失败 start=%.1fs: %s", start, exc)
            continue
        finally:
            clip.unlink(missing_ok=True)
        if not embedding:
            continue
        samples.append(
            vp.SpeakerSample(
                start_ms=int((start + lead_in) * 1000),
                end_ms=int((start + lead_in + length) * 1000),
                embedding=embedding,
                rms=energy,
            )
        )
    return samples


async def _propose(
    speakers: list[_NamedSpeaker],
    *,
    minute_token: str,
    owner_user_id: int,
) -> tuple[int, int]:
    """与全局库比对并落提案。返回（新提案数, 已同名跳过数）。"""
    threshold = vp.match_threshold()
    proposals = 0
    skipped = 0

    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        # 同一场会议重跑时先清掉上一轮没人处理的提案，免得越堆越多
        await uow.voiceprints.clear_pending_proposals(
            minute_token, owner_user_id=owner_user_id
        )
        known = [
            (person, vp.decode_embedding(person.embedding))
            for person in await uow.voiceprints.list_active()
        ]

        for speaker in speakers:
            best_person = None
            best_score = 0.0
            for person, vector in known:
                score = vp.cosine(speaker.centroid, vector)
                if score >= threshold and score > best_score:
                    best_person, best_score = person, score

            if best_person is not None and (
                best_person.display_name or ""
            ).strip() == speaker.name:
                # 已经认识而且名字就是这个，补几段样本就够了
                for sample in speaker.samples:
                    await uow.voiceprints.add_sample(
                        VoiceprintSampleCreateEntity(
                            voiceprint_id=best_person.id,
                            owner_user_id=owner_user_id,
                            minute_token=minute_token,
                            start_ms=sample.start_ms,
                            end_ms=sample.end_ms,
                            score=best_score,
                        )
                    )
                skipped += 1
                continue

            await uow.voiceprints.add_proposal(
                VoiceprintNameProposalCreateEntity(
                    owner_user_id=owner_user_id,
                    minute_token=minute_token,
                    proposed_name=speaker.name,
                    voiceprint_id=best_person.id if best_person else None,
                    embedding=vp.encode_embedding(speaker.centroid),
                    dim=len(speaker.centroid),
                    score=best_score if best_person else None,
                    sample_count=len(speaker.samples),
                    samples=[
                        ProposalSample(
                            start_ms=s.start_ms, end_ms=s.end_ms, score=None
                        )
                        for s in speaker.samples
                    ],
                )
            )
            proposals += 1
        await uow.commit()
    return proposals, skipped


async def approve_proposal(proposal_id: int) -> VoiceprintNameProposalEntity:
    """批准提案：给既有人物写上名字并合并质心，或建一个新人物。"""
    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        proposal = await uow.voiceprints.get_proposal(proposal_id)
        if proposal is None:
            raise VoiceprintHarvestError("提案不存在")
        if proposal.status != PROPOSAL_PENDING:
            raise VoiceprintHarvestError("这条提案已经处理过了")

        centroid = vp.decode_embedding(proposal.embedding)
        target_id = proposal.voiceprint_id
        if target_id is not None:
            person = await uow.voiceprints.resolve(target_id)
            if person is None:
                raise VoiceprintHarvestError("提案指向的人物已被删除")
            merged = vp.weighted_merge(
                vp.decode_embedding(person.embedding),
                min(person.sample_count or 1, CENTROID_WEIGHT_CAP),
                centroid,
                max(1, proposal.sample_count),
            )
            await uow.voiceprints.update(
                VoiceprintUpdateEntity(
                    id=person.id,
                    display_name=proposal.proposed_name,
                    embedding=vp.encode_embedding(merged),
                    sample_count=(person.sample_count or 0)
                    + proposal.sample_count,
                    meeting_count=(person.meeting_count or 0) + 1,
                )
            )
            target_id = person.id
        else:
            created = await uow.voiceprints.create(
                VoiceprintCreateEntity(
                    embedding=proposal.embedding,
                    dim=proposal.dim,
                    sample_count=proposal.sample_count,
                    meeting_count=1,
                    display_name=proposal.proposed_name,
                    note=f"由 {proposal.minute_token} 的飞书转写姓名自动提炼",
                )
            )
            target_id = created.id

        for sample in proposal.samples:
            await uow.voiceprints.add_sample(
                VoiceprintSampleCreateEntity(
                    voiceprint_id=target_id,
                    owner_user_id=proposal.owner_user_id,
                    minute_token=proposal.minute_token,
                    start_ms=sample.start_ms,
                    end_ms=sample.end_ms,
                    score=proposal.score,
                )
            )
        updated = await uow.voiceprints.set_proposal_status(
            proposal_id, PROPOSAL_APPROVED
        )
        await uow.commit()

    from app.service.transcription_flow import refresh_speakers_json

    await refresh_speakers_json(
        proposal.minute_token, owner_user_id=proposal.owner_user_id
    )
    logger.info(
        "已批准声纹命名提案 id=%s name=%s voiceprint=%s",
        proposal_id,
        proposal.proposed_name,
        target_id,
    )
    assert updated is not None
    return updated


async def reject_proposal(proposal_id: int) -> VoiceprintNameProposalEntity:
    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        proposal = await uow.voiceprints.get_proposal(proposal_id)
        if proposal is None:
            raise VoiceprintHarvestError("提案不存在")
        updated = await uow.voiceprints.set_proposal_status(
            proposal_id, PROPOSAL_REJECTED
        )
        await uow.commit()
    assert updated is not None
    return updated
