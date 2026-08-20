from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.meeting_record import (
    MeetingRecordCreateEntity,
    MeetingRecordEntity,
    MeetingRecordQueryEntity,
    MeetingRecordUpdateEntity,
)
from app.data_model.unset import is_set
from app.repository.orm.meeting_record import MeetingRecordORM


def _to_entity(orm: MeetingRecordORM) -> MeetingRecordEntity:
    return MeetingRecordEntity(
        id=orm.id,
        feishu_event_id=orm.feishu_event_id,
        event_type=orm.event_type,
        unique_key=orm.unique_key,
        minute_token=orm.minute_token,
        meeting_id=orm.meeting_id,
        title=orm.title,
        duration_ms=orm.duration_ms,
        has_video=orm.has_video,
        status=orm.status,
        error_message=orm.error_message,
        storage_path=orm.storage_path,
        owner_user_id=orm.owner_user_id,
        summary_status=orm.summary_status,
        media_relpath=orm.media_relpath,
        transcript_relpath=orm.transcript_relpath,
        downloaded_at=orm.downloaded_at,
        storage_root_relpath=orm.storage_root_relpath,
        speakers_json=orm.speakers_json,
        create_time=orm.create_time,
        transcript_source=orm.transcript_source,
        transcript_coverage=orm.transcript_coverage,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class MeetingRecordRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, entity: MeetingRecordCreateEntity) -> MeetingRecordEntity:
        orm = MeetingRecordORM(
            feishu_event_id=entity.feishu_event_id,
            event_type=entity.event_type,
            unique_key=entity.unique_key,
            minute_token=entity.minute_token,
            meeting_id=entity.meeting_id,
            title=entity.title,
            duration_ms=entity.duration_ms,
            has_video=entity.has_video,
            status=entity.status,
            error_message=entity.error_message,
            storage_path=entity.storage_path,
            owner_user_id=entity.owner_user_id,
            summary_status=entity.summary_status,
            media_relpath=entity.media_relpath,
            transcript_relpath=entity.transcript_relpath,
            downloaded_at=entity.downloaded_at,
            storage_root_relpath=entity.storage_root_relpath,
            speakers_json=entity.speakers_json,
            create_time=entity.create_time,
            transcript_source=entity.transcript_source,
            transcript_coverage=entity.transcript_coverage,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def update(self, entity: MeetingRecordUpdateEntity) -> MeetingRecordEntity | None:
        orm = await self._session.get(MeetingRecordORM, entity.id)
        if orm is None:
            return None
        for field in (
            "minute_token",
            "meeting_id",
            "title",
            "duration_ms",
            "has_video",
            "status",
            "error_message",
            "storage_path",
            "owner_user_id",
            "summary_status",
            "media_relpath",
            "transcript_relpath",
            "downloaded_at",
            "storage_root_relpath",
            "unique_key",
            "speakers_json",
            "event_type",
            "feishu_event_id",
            "create_time",
            "transcript_source",
            "transcript_coverage",
        ):
            value = getattr(entity, field)
            if is_set(value):
                setattr(orm, field, value)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def delete_by_ids(self, ids: list[int]) -> int:
        if not ids:
            return 0
        deleted = 0
        for record_id in ids:
            orm = await self._session.get(MeetingRecordORM, record_id)
            if orm is None:
                continue
            await self._session.delete(orm)
            deleted += 1
        await self._session.flush()
        return deleted

    async def list_all_with_token(self) -> list[MeetingRecordEntity]:
        stmt = (
            select(MeetingRecordORM)
            .where(MeetingRecordORM.minute_token.is_not(None))
            .order_by(MeetingRecordORM.id.desc())
        )
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def upsert_by_minute_token(
        self, entity: MeetingRecordCreateEntity
    ) -> MeetingRecordEntity:
        """按 (owner, minute_token) 合并会议主档；无 feishu_event_id 冲突时用于文件回填。"""
        token = (entity.minute_token or "").strip()
        if not token:
            return await self.create(entity)
        if entity.owner_user_id is None:
            raise ValueError("upsert_by_minute_token 需要 owner_user_id")
        existing = await self.get_latest_by_minute_token(
            token, owner_user_id=int(entity.owner_user_id)
        )
        if existing is None or existing.id is None:
            return await self.create(entity)
        # 建档实体里没填的字段一律是 None，合并时当作「没提」，不能拿它去抹已有值
        fields = {
            name: value
            for name, value in (
                ("minute_token", entity.minute_token),
                ("meeting_id", entity.meeting_id),
                ("title", entity.title),
                ("duration_ms", entity.duration_ms),
                ("has_video", entity.has_video),
                ("status", entity.status),
                ("error_message", entity.error_message),
                ("storage_path", entity.storage_path),
                ("owner_user_id", entity.owner_user_id),
                ("summary_status", entity.summary_status),
                ("media_relpath", entity.media_relpath),
                ("transcript_relpath", entity.transcript_relpath),
                ("downloaded_at", entity.downloaded_at),
                ("storage_root_relpath", entity.storage_root_relpath),
                ("unique_key", entity.unique_key),
                ("speakers_json", entity.speakers_json),
                ("create_time", entity.create_time),
                ("transcript_source", entity.transcript_source),
                ("transcript_coverage", entity.transcript_coverage),
            )
            if value is not None
        }
        updated = await self.update(
            MeetingRecordUpdateEntity(id=existing.id, **fields)
        )
        return updated or existing

    async def get_by_id(self, record_id: int) -> MeetingRecordEntity | None:
        orm = await self._session.get(MeetingRecordORM, record_id)
        return _to_entity(orm) if orm else None

    async def get_by_event_id(self, event_id: str) -> MeetingRecordEntity | None:
        stmt = select(MeetingRecordORM).where(MeetingRecordORM.feishu_event_id == event_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def get_latest_by_minute_token(
        self, minute_token: str, *, owner_user_id: int
    ) -> MeetingRecordEntity | None:
        stmt = (
            select(MeetingRecordORM)
            .where(
                MeetingRecordORM.minute_token == minute_token,
                MeetingRecordORM.owner_user_id == int(owner_user_id),
            )
            .order_by(MeetingRecordORM.id.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_entity(orm) if orm else None

    async def map_latest_by_minute_tokens(
        self,
        minute_tokens: list[str],
        *,
        owner_user_id: int | None = None,
    ) -> dict[str, MeetingRecordEntity]:
        if not minute_tokens:
            return {}
        stmt = (
            select(MeetingRecordORM)
            .where(MeetingRecordORM.minute_token.in_(minute_tokens))
            .order_by(MeetingRecordORM.id.desc())
        )
        if owner_user_id is not None:
            stmt = stmt.where(MeetingRecordORM.owner_user_id == owner_user_id)
        result = await self._session.execute(stmt)
        mapping: dict[str, MeetingRecordEntity] = {}
        for orm in result.scalars().all():
            if orm.minute_token and orm.minute_token not in mapping:
                mapping[orm.minute_token] = _to_entity(orm)
        return mapping

    async def query(self, query: MeetingRecordQueryEntity) -> list[MeetingRecordEntity]:
        stmt = select(MeetingRecordORM)
        if query.id is not None:
            stmt = stmt.where(MeetingRecordORM.id == query.id)
        if query.feishu_event_id is not None:
            stmt = stmt.where(MeetingRecordORM.feishu_event_id == query.feishu_event_id)
        if query.minute_token is not None:
            stmt = stmt.where(MeetingRecordORM.minute_token == query.minute_token)
        if query.unique_key is not None:
            stmt = stmt.where(MeetingRecordORM.unique_key == query.unique_key)
        if query.status is not None:
            stmt = stmt.where(MeetingRecordORM.status == query.status)
        if query.summary_status is not None:
            stmt = stmt.where(MeetingRecordORM.summary_status == query.summary_status)
        if query.owner_user_id is not None:
            stmt = stmt.where(MeetingRecordORM.owner_user_id == query.owner_user_id)
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def list_latest_by_scope(
        self,
        *,
        owner_user_id: int | None = None,
        limit: int = 500,
    ) -> list[MeetingRecordEntity]:
        """取每个资源最新一条。

        - 普通 scope：同一 owner 下按 minute_token 去重
        - 超管全站（owner_user_id is None）：按 (owner_user_id, minute_token) 去重
        """
        stmt = (
            select(MeetingRecordORM)
            .where(MeetingRecordORM.minute_token.is_not(None))
            .order_by(MeetingRecordORM.id.desc())
        )
        if owner_user_id is not None:
            stmt = stmt.where(MeetingRecordORM.owner_user_id == owner_user_id)
        result = await self._session.execute(stmt)
        latest: dict[tuple[int | None, str], MeetingRecordEntity] = {}
        for orm in result.scalars().all():
            token = orm.minute_token
            if not token:
                continue
            # 超管全站必须保留不同 owner 的同 token；用户 scope 已按 owner 过滤
            key = (
                (orm.owner_user_id, token)
                if owner_user_id is None
                else (None, token)
            )
            if key in latest:
                continue
            latest[key] = _to_entity(orm)
            if len(latest) >= max(1, limit):
                break
        return list(latest.values())

    async def fail_stale_inflight(
        self,
        *,
        cutoff: datetime,
        download_error: str,
        summary_error: str,
    ) -> tuple[list[MeetingRecordEntity], list[MeetingRecordEntity]]:
        """将超时仍为 DOWNLOADING / summary GENERATING 的记录标为 FAILED。"""
        download_stmt = select(MeetingRecordORM).where(
            MeetingRecordORM.status == "DOWNLOADING",
            MeetingRecordORM.updated_at < cutoff,
        )
        download_result = await self._session.execute(download_stmt)
        download_failed: list[MeetingRecordEntity] = []
        for orm in download_result.scalars().all():
            orm.status = "FAILED"
            orm.error_message = download_error
            download_failed.append(_to_entity(orm))

        summary_stmt = select(MeetingRecordORM).where(
            MeetingRecordORM.summary_status == "GENERATING",
            MeetingRecordORM.updated_at < cutoff,
        )
        summary_result = await self._session.execute(summary_stmt)
        summary_failed: list[MeetingRecordEntity] = []
        for orm in summary_result.scalars().all():
            orm.summary_status = "FAILED"
            if not orm.error_message:
                orm.error_message = summary_error
            summary_failed.append(_to_entity(orm))

        if download_failed or summary_failed:
            await self._session.flush()
        return download_failed, summary_failed
