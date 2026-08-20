from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.voiceprint import (
    PROPOSAL_PENDING,
    MeetingSpeakerEntity,
    MeetingSpeakerUpsertEntity,
    ProposalSample,
    VoiceprintCreateEntity,
    VoiceprintEntity,
    VoiceprintNameProposalCreateEntity,
    VoiceprintNameProposalEntity,
    VoiceprintSampleCreateEntity,
    VoiceprintSampleEntity,
    VoiceprintUpdateEntity,
)
from app.repository.json_map import dump_json, load_json_list
from app.repository.orm.voiceprint import (
    MeetingSpeakerORM,
    VoiceprintNameProposalORM,
    VoiceprintORM,
    VoiceprintSampleORM,
)


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _to_voiceprint(orm: VoiceprintORM) -> VoiceprintEntity:
    return VoiceprintEntity(
        id=orm.id,
        display_name=orm.display_name,
        embedding=orm.embedding,
        dim=orm.dim,
        sample_count=orm.sample_count or 0,
        meeting_count=orm.meeting_count or 0,
        note=orm.note,
        merged_into=orm.merged_into,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


def _to_sample(orm: VoiceprintSampleORM) -> VoiceprintSampleEntity:
    return VoiceprintSampleEntity(
        id=orm.id,
        voiceprint_id=orm.voiceprint_id,
        owner_user_id=orm.owner_user_id,
        minute_token=orm.minute_token,
        start_ms=orm.start_ms,
        end_ms=orm.end_ms,
        score=orm.score,
        created_at=orm.created_at,
    )


def _to_proposal(
    orm: VoiceprintNameProposalORM,
) -> VoiceprintNameProposalEntity:
    samples: list[ProposalSample] = []
    for item in load_json_list(orm.samples_json):
        if not isinstance(item, dict):
            continue
        samples.append(
            ProposalSample(
                start_ms=int(item.get("start_ms") or 0),
                end_ms=int(item.get("end_ms") or 0),
                score=item.get("score"),
            )
        )
    return VoiceprintNameProposalEntity(
        id=orm.id,
        owner_user_id=orm.owner_user_id,
        minute_token=orm.minute_token,
        proposed_name=orm.proposed_name,
        voiceprint_id=orm.voiceprint_id,
        embedding=orm.embedding,
        dim=orm.dim,
        score=orm.score,
        sample_count=orm.sample_count or 0,
        samples=samples,
        status=orm.status,
        created_at=orm.created_at,
        decided_at=orm.decided_at,
    )


def _to_speaker(orm: MeetingSpeakerORM) -> MeetingSpeakerEntity:
    return MeetingSpeakerEntity(
        id=orm.id,
        owner_user_id=orm.owner_user_id,
        minute_token=orm.minute_token,
        local_label=orm.local_label,
        cloud_ids=[str(x) for x in load_json_list(orm.cloud_ids_json)],
        voiceprint_id=orm.voiceprint_id,
        match_score=orm.match_score,
        talk_ms=orm.talk_ms or 0,
        segments=orm.segments or 0,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class VoiceprintRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------- 人物 ----------

    async def list_active(self) -> list[VoiceprintEntity]:
        stmt = (
            select(VoiceprintORM)
            .where(VoiceprintORM.merged_into.is_(None))
            .order_by(VoiceprintORM.id)
        )
        result = await self._session.execute(stmt)
        return [_to_voiceprint(orm) for orm in result.scalars().all()]

    async def get(self, voiceprint_id: int) -> VoiceprintEntity | None:
        orm = await self._session.get(VoiceprintORM, voiceprint_id)
        return _to_voiceprint(orm) if orm else None

    async def resolve(self, voiceprint_id: int) -> VoiceprintEntity | None:
        """跟随 merged_into 找到最终有效的人物，防止指向已合并的旧行。"""
        seen: set[int] = set()
        current = voiceprint_id
        while current not in seen:
            seen.add(current)
            entity = await self.get(current)
            if entity is None or entity.merged_into is None:
                return entity
            current = entity.merged_into
        return None

    async def create(self, entity: VoiceprintCreateEntity) -> VoiceprintEntity:
        now = _now_ms()
        orm = VoiceprintORM(
            display_name=entity.display_name,
            embedding=entity.embedding,
            dim=entity.dim,
            sample_count=entity.sample_count,
            meeting_count=entity.meeting_count,
            note=entity.note,
            created_at=now,
            updated_at=now,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_voiceprint(orm)

    async def update(self, entity: VoiceprintUpdateEntity) -> VoiceprintEntity | None:
        orm = await self._session.get(VoiceprintORM, entity.id)
        if orm is None:
            return None
        if entity.display_name is not None:
            orm.display_name = entity.display_name.strip() or None
        if entity.embedding is not None:
            orm.embedding = entity.embedding
        if entity.sample_count is not None:
            orm.sample_count = entity.sample_count
        if entity.meeting_count is not None:
            orm.meeting_count = entity.meeting_count
        if entity.note is not None:
            orm.note = entity.note
        if entity.merged_into is not None:
            orm.merged_into = entity.merged_into
        orm.updated_at = _now_ms()
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_voiceprint(orm)

    async def delete(self, voiceprint_id: int) -> bool:
        orm = await self._session.get(VoiceprintORM, voiceprint_id)
        if orm is None:
            return False
        await self._session.delete(orm)
        await self._session.execute(
            delete(VoiceprintSampleORM).where(
                VoiceprintSampleORM.voiceprint_id == voiceprint_id
            )
        )
        # 引用它的会议说话人退回未识别状态，转写里就回到「说话人N」
        await self._session.execute(
            update(MeetingSpeakerORM)
            .where(MeetingSpeakerORM.voiceprint_id == voiceprint_id)
            .values(voiceprint_id=None, match_score=None, updated_at=_now_ms())
        )
        await self._session.flush()
        return True

    async def repoint(self, source_id: int, target_id: int) -> None:
        """把源人物的样本与会议引用改挂到目标人物。"""
        now = _now_ms()
        await self._session.execute(
            update(VoiceprintSampleORM)
            .where(VoiceprintSampleORM.voiceprint_id == source_id)
            .values(voiceprint_id=target_id)
        )
        await self._session.execute(
            update(MeetingSpeakerORM)
            .where(MeetingSpeakerORM.voiceprint_id == source_id)
            .values(voiceprint_id=target_id, updated_at=now)
        )
        await self._session.flush()

    # ---------- 样本 ----------

    async def add_sample(
        self, entity: VoiceprintSampleCreateEntity
    ) -> VoiceprintSampleEntity:
        orm = VoiceprintSampleORM(
            voiceprint_id=entity.voiceprint_id,
            owner_user_id=entity.owner_user_id,
            minute_token=entity.minute_token,
            start_ms=entity.start_ms,
            end_ms=entity.end_ms,
            score=entity.score,
            created_at=_now_ms(),
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_sample(orm)

    async def list_samples(
        self, voiceprint_id: int, *, limit: int = 6
    ) -> list[VoiceprintSampleEntity]:
        stmt = (
            select(VoiceprintSampleORM)
            .where(VoiceprintSampleORM.voiceprint_id == voiceprint_id)
            .order_by(VoiceprintSampleORM.score.desc().nullslast())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_to_sample(orm) for orm in result.scalars().all()]

    async def count_samples(self, voiceprint_ids: list[int]) -> dict[int, int]:
        if not voiceprint_ids:
            return {}
        stmt = select(VoiceprintSampleORM.voiceprint_id).where(
            VoiceprintSampleORM.voiceprint_id.in_(voiceprint_ids)
        )
        result = await self._session.execute(stmt)
        counts: dict[int, int] = {}
        for (vid,) in result.all():
            counts[vid] = counts.get(vid, 0) + 1
        return counts

    # ---------- 会议说话人 ----------

    async def list_speakers(
        self, minute_token: str, *, owner_user_id: int
    ) -> list[MeetingSpeakerEntity]:
        stmt = (
            select(MeetingSpeakerORM)
            .where(
                MeetingSpeakerORM.owner_user_id == owner_user_id,
                MeetingSpeakerORM.minute_token == minute_token,
            )
            .order_by(MeetingSpeakerORM.id)
        )
        result = await self._session.execute(stmt)
        return [_to_speaker(orm) for orm in result.scalars().all()]

    async def list_speakers_by_voiceprint(
        self, voiceprint_id: int, *, limit: int = 50
    ) -> list[MeetingSpeakerEntity]:
        stmt = (
            select(MeetingSpeakerORM)
            .where(MeetingSpeakerORM.voiceprint_id == voiceprint_id)
            .order_by(MeetingSpeakerORM.talk_ms.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_to_speaker(orm) for orm in result.scalars().all()]

    async def upsert_speaker(
        self, entity: MeetingSpeakerUpsertEntity
    ) -> MeetingSpeakerEntity:
        stmt = select(MeetingSpeakerORM).where(
            MeetingSpeakerORM.owner_user_id == entity.owner_user_id,
            MeetingSpeakerORM.minute_token == entity.minute_token,
            MeetingSpeakerORM.local_label == entity.local_label,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        now = _now_ms()
        if orm is None:
            orm = MeetingSpeakerORM(
                owner_user_id=entity.owner_user_id,
                minute_token=entity.minute_token,
                local_label=entity.local_label,
                created_at=now,
            )
            self._session.add(orm)
        orm.cloud_ids_json = dump_json(entity.cloud_ids, default="[]")
        orm.voiceprint_id = entity.voiceprint_id
        orm.match_score = entity.match_score
        orm.talk_ms = entity.talk_ms
        orm.segments = entity.segments
        orm.updated_at = now
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_speaker(orm)

    async def clear_speakers(self, minute_token: str, *, owner_user_id: int) -> None:
        await self._session.execute(
            delete(MeetingSpeakerORM).where(
                MeetingSpeakerORM.owner_user_id == owner_user_id,
                MeetingSpeakerORM.minute_token == minute_token,
            )
        )
        await self._session.flush()

    async def clear_samples_of_meeting(
        self, minute_token: str, *, owner_user_id: int
    ) -> None:
        await self._session.execute(
            delete(VoiceprintSampleORM).where(
                VoiceprintSampleORM.owner_user_id == owner_user_id,
                VoiceprintSampleORM.minute_token == minute_token,
            )
        )
        await self._session.flush()

    # ---------- 命名提案 ----------

    async def add_proposal(
        self, entity: VoiceprintNameProposalCreateEntity
    ) -> VoiceprintNameProposalEntity:
        orm = VoiceprintNameProposalORM(
            owner_user_id=entity.owner_user_id,
            minute_token=entity.minute_token,
            proposed_name=entity.proposed_name,
            voiceprint_id=entity.voiceprint_id,
            embedding=entity.embedding,
            dim=entity.dim,
            score=entity.score,
            sample_count=entity.sample_count,
            samples_json=dump_json(
                [
                    {
                        "start_ms": s.start_ms,
                        "end_ms": s.end_ms,
                        "score": s.score,
                    }
                    for s in entity.samples
                ],
                default="[]",
            ),
            status=PROPOSAL_PENDING,
            created_at=_now_ms(),
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_proposal(orm)

    async def get_proposal(
        self, proposal_id: int
    ) -> VoiceprintNameProposalEntity | None:
        orm = await self._session.get(VoiceprintNameProposalORM, proposal_id)
        return _to_proposal(orm) if orm else None

    async def list_proposals(
        self,
        *,
        status: str | None = PROPOSAL_PENDING,
        owner_user_id: int | None = None,
        minute_token: str | None = None,
        limit: int = 200,
    ) -> list[VoiceprintNameProposalEntity]:
        stmt = select(VoiceprintNameProposalORM)
        if status is not None:
            stmt = stmt.where(VoiceprintNameProposalORM.status == status)
        if owner_user_id is not None:
            stmt = stmt.where(
                VoiceprintNameProposalORM.owner_user_id == owner_user_id
            )
        if minute_token is not None:
            stmt = stmt.where(
                VoiceprintNameProposalORM.minute_token == minute_token
            )
        stmt = stmt.order_by(VoiceprintNameProposalORM.id.desc()).limit(limit)
        result = await self._session.execute(stmt)
        return [_to_proposal(orm) for orm in result.scalars().all()]

    async def set_proposal_status(
        self, proposal_id: int, status: str
    ) -> VoiceprintNameProposalEntity | None:
        orm = await self._session.get(VoiceprintNameProposalORM, proposal_id)
        if orm is None:
            return None
        orm.status = status
        orm.decided_at = _now_ms()
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_proposal(orm)

    async def clear_pending_proposals(
        self, minute_token: str, *, owner_user_id: int
    ) -> None:
        """同一场会议重跑提炼前先清掉上一轮没人处理的提案，避免堆重复。"""
        await self._session.execute(
            delete(VoiceprintNameProposalORM).where(
                VoiceprintNameProposalORM.owner_user_id == owner_user_id,
                VoiceprintNameProposalORM.minute_token == minute_token,
                VoiceprintNameProposalORM.status == PROPOSAL_PENDING,
            )
        )
        await self._session.flush()
