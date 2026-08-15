"""超级管理端：全局声纹人物库的命名、合并与删除。

库是全站共享的（同一个人在谁的会议里都该认得出来），所以命名权只给超管，
普通管理员只看得到结果。改名不回写任何转写文件，展示时现查现换。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth_context import require_super_admin
from app.data_model.entity.voiceprint import VoiceprintUpdateEntity
from app.repository.uow import UnitOfWork
from app.service import voiceprint_service as vp
from app.service.ownership import assert_meeting_readable
from app.service.transcription_flow import refresh_speakers_json

router = APIRouter(prefix="/admin/voiceprints", tags=["admin-voiceprints"])


class VoiceprintSampleResponse(BaseModel):
    minute_token: str
    owner_user_id: int
    start_ms: int
    end_ms: int
    score: float | None = None
    audio_url: str | None = None


class VoiceprintMeetingResponse(BaseModel):
    minute_token: str
    owner_user_id: int
    title: str | None = None
    local_label: str
    talk_ms: int
    match_score: float | None = None


class VoiceprintItemResponse(BaseModel):
    id: int
    display_name: str | None = None
    named: bool
    sample_count: int
    meeting_count: int
    note: str | None = None
    updated_at: int
    updated_at_text: str
    samples: list[VoiceprintSampleResponse] = []
    meetings: list[VoiceprintMeetingResponse] = []


class VoiceprintListResponse(BaseModel):
    items: list[VoiceprintItemResponse]
    total: int


class MeetingSpeakerItemResponse(BaseModel):
    local_label: str
    voiceprint_id: int | None = None
    display_name: str | None = None
    talk_ms: int = 0
    segments: int = 0
    match_score: float | None = None


class MeetingSpeakerListResponse(BaseModel):
    items: list[MeetingSpeakerItemResponse]
    total: int


class VoiceprintRenameRequest(BaseModel):
    display_name: str = Field(default="", max_length=128)
    note: str | None = None


class VoiceprintMergeRequest(BaseModel):
    source_ids: list[int] = Field(..., min_length=1)


def _ms_text(ms: int) -> str:
    if not ms:
        return "—"
    value = ms / 1000.0 if ms > 10_000_000_000 else float(ms)
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )


async def _load_items(*, with_audio: bool) -> list[VoiceprintItemResponse]:
    from app.service.r2_media_service import r2_media_service

    items: list[VoiceprintItemResponse] = []
    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        assert uow.meeting_records is not None
        people = await uow.voiceprints.list_active()
        for person in people:
            samples = await uow.voiceprints.list_samples(person.id)
            speakers = await uow.voiceprints.list_speakers_by_voiceprint(person.id)
            titles: dict[tuple[int, str], str | None] = {}
            for speaker in speakers:
                record = await uow.meeting_records.get_latest_by_minute_token(
                    speaker.minute_token, owner_user_id=speaker.owner_user_id
                )
                titles[(speaker.owner_user_id, speaker.minute_token)] = (
                    record.title if record else None
                )
            sample_items: list[VoiceprintSampleResponse] = []
            for sample in samples:
                url = None
                if with_audio:
                    url = await r2_media_service.presign_asr_audio(
                        sample.minute_token, owner_user_id=sample.owner_user_id
                    )
                sample_items.append(
                    VoiceprintSampleResponse(
                        minute_token=sample.minute_token,
                        owner_user_id=sample.owner_user_id,
                        start_ms=sample.start_ms,
                        end_ms=sample.end_ms,
                        score=sample.score,
                        audio_url=url,
                    )
                )
            items.append(
                VoiceprintItemResponse(
                    id=person.id,
                    display_name=person.display_name,
                    named=person.named,
                    sample_count=person.sample_count,
                    meeting_count=person.meeting_count,
                    note=person.note,
                    updated_at=person.updated_at,
                    updated_at_text=_ms_text(person.updated_at),
                    samples=sample_items,
                    meetings=[
                        VoiceprintMeetingResponse(
                            minute_token=speaker.minute_token,
                            owner_user_id=speaker.owner_user_id,
                            title=titles.get(
                                (speaker.owner_user_id, speaker.minute_token)
                            ),
                            local_label=speaker.local_label,
                            talk_ms=speaker.talk_ms,
                            match_score=speaker.match_score,
                        )
                        for speaker in speakers
                    ],
                )
            )
    return items


async def _refresh_affected(person_ids: list[int]) -> None:
    """人物改动后，把引用它的会议参会人列表重刷一遍。"""
    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        pairs: set[tuple[str, int]] = set()
        for person_id in person_ids:
            for speaker in await uow.voiceprints.list_speakers_by_voiceprint(person_id):
                pairs.add((speaker.minute_token, speaker.owner_user_id))
    for token, owner in pairs:
        await refresh_speakers_json(token, owner_user_id=owner)


@router.get("", response_model=VoiceprintListResponse)
async def list_voiceprints(request: Request) -> VoiceprintListResponse:
    require_super_admin(request)
    items = await _load_items(with_audio=True)
    return VoiceprintListResponse(items=items, total=len(items))


@router.get("/meeting/{minute_token}", response_model=MeetingSpeakerListResponse)
async def list_meeting_speakers(
    minute_token: str, request: Request, owner_user_id: int | None = None
) -> MeetingSpeakerListResponse:
    """详情页的就地改名入口：列出这场会议的说话人与对应人物。"""
    require_super_admin(request)
    owner_user_id = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )
    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        speakers = await uow.voiceprints.list_speakers(
            minute_token, owner_user_id=owner_user_id
        )
        items: list[MeetingSpeakerItemResponse] = []
        for speaker in speakers:
            person = (
                await uow.voiceprints.resolve(speaker.voiceprint_id)
                if speaker.voiceprint_id is not None
                else None
            )
            items.append(
                MeetingSpeakerItemResponse(
                    local_label=speaker.local_label,
                    voiceprint_id=person.id if person else None,
                    display_name=person.display_name if person else None,
                    talk_ms=speaker.talk_ms,
                    segments=speaker.segments,
                    match_score=speaker.match_score,
                )
            )
    return MeetingSpeakerListResponse(items=items, total=len(items))


@router.patch("/{voiceprint_id}", response_model=VoiceprintItemResponse)
async def rename_voiceprint(
    voiceprint_id: int, body: VoiceprintRenameRequest, request: Request
) -> VoiceprintItemResponse:
    require_super_admin(request)
    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        updated = await uow.voiceprints.update(
            VoiceprintUpdateEntity(
                id=voiceprint_id,
                display_name=body.display_name.strip(),
                note=body.note,
            )
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="声纹人物不存在")
        await uow.commit()
    await _refresh_affected([voiceprint_id])
    items = await _load_items(with_audio=False)
    for item in items:
        if item.id == voiceprint_id:
            return item
    raise HTTPException(status_code=404, detail="声纹人物不存在")


@router.post("/{voiceprint_id}/merge", response_model=VoiceprintItemResponse)
async def merge_voiceprints(
    voiceprint_id: int, body: VoiceprintMergeRequest, request: Request
) -> VoiceprintItemResponse:
    """把若干人物并进目标人物：样本与会议改挂过去，质心按样本数加权重算。"""
    require_super_admin(request)
    sources = [i for i in body.source_ids if i != voiceprint_id]
    if not sources:
        raise HTTPException(status_code=400, detail="没有可合并的来源人物")
    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        target = await uow.voiceprints.get(voiceprint_id)
        if target is None or target.merged_into is not None:
            raise HTTPException(status_code=404, detail="目标声纹人物不存在")
        embedding = vp.decode_embedding(target.embedding)
        weight = max(1, target.sample_count)
        meetings = target.meeting_count
        for source_id in sources:
            source = await uow.voiceprints.get(source_id)
            if source is None or source.merged_into is not None:
                continue
            embedding = vp.weighted_merge(
                embedding,
                weight,
                vp.decode_embedding(source.embedding),
                max(1, source.sample_count),
            )
            weight += max(1, source.sample_count)
            meetings += source.meeting_count
            await uow.voiceprints.repoint(source_id, voiceprint_id)
            await uow.voiceprints.update(
                VoiceprintUpdateEntity(id=source_id, merged_into=voiceprint_id)
            )
        await uow.voiceprints.update(
            VoiceprintUpdateEntity(
                id=voiceprint_id,
                embedding=vp.encode_embedding(embedding),
                sample_count=weight,
                meeting_count=meetings,
            )
        )
        await uow.commit()
    await _refresh_affected([voiceprint_id])
    items = await _load_items(with_audio=False)
    for item in items:
        if item.id == voiceprint_id:
            return item
    raise HTTPException(status_code=404, detail="声纹人物不存在")


@router.delete("/{voiceprint_id}")
async def delete_voiceprint(voiceprint_id: int, request: Request) -> dict[str, bool]:
    """删掉认错的人物；引用它的会议退回「说话人N」，下次转写会重新建条目。"""
    require_super_admin(request)
    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        affected = [
            (s.minute_token, s.owner_user_id)
            for s in await uow.voiceprints.list_speakers_by_voiceprint(voiceprint_id)
        ]
        ok = await uow.voiceprints.delete(voiceprint_id)
        if not ok:
            raise HTTPException(status_code=404, detail="声纹人物不存在")
        await uow.commit()
    for token, owner in affected:
        await refresh_speakers_json(token, owner_user_id=owner)
    return {"ok": True}
