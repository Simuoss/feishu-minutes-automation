from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_model.entity.share_access_log import (
    ShareAccessLogCreateEntity,
    ShareAccessLogEntity,
)
from app.repository.orm.share_access_log import ShareAccessLogORM


def _to_entity(orm: ShareAccessLogORM) -> ShareAccessLogEntity:
    return ShareAccessLogEntity(
        id=orm.id,
        access_key_id=orm.access_key_id,
        share_id=orm.share_id,
        minute_token=orm.minute_token,
        action=orm.action,
        ip=orm.ip,
        user_agent=orm.user_agent,
        created_at=orm.created_at,
        meeting_title=orm.meeting_title,
        session_id=orm.session_id,
        referer=orm.referer,
        device_type=orm.device_type,
        browser=orm.browser,
        os=orm.os,
        started_at=orm.started_at,
        ended_at=orm.ended_at,
        dwell_ms=orm.dwell_ms,
        video_progress_pct=orm.video_progress_pct,
        result=orm.result,
        fail_reason=orm.fail_reason,
        detail_json=orm.detail_json,
    )


class ShareAccessLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, entity: ShareAccessLogCreateEntity) -> ShareAccessLogEntity:
        orm = ShareAccessLogORM(
            access_key_id=entity.access_key_id,
            share_id=entity.share_id,
            minute_token=entity.minute_token,
            meeting_title=entity.meeting_title,
            action=entity.action,
            ip=entity.ip,
            user_agent=entity.user_agent,
            created_at=entity.created_at,
            session_id=entity.session_id,
            referer=entity.referer,
            device_type=entity.device_type,
            browser=entity.browser,
            os=entity.os,
            started_at=entity.started_at,
            ended_at=entity.ended_at,
            dwell_ms=entity.dwell_ms,
            video_progress_pct=entity.video_progress_pct,
            result=entity.result,
            fail_reason=entity.fail_reason,
            detail_json=entity.detail_json,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return _to_entity(orm)

    async def list_by_key(
        self, access_key_id: int, *, limit: int = 100, offset: int = 0
    ) -> list[ShareAccessLogEntity]:
        stmt = (
            select(ShareAccessLogORM)
            .where(ShareAccessLogORM.access_key_id == access_key_id)
            .order_by(ShareAccessLogORM.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def list_by_share(
        self, share_id: int, *, limit: int = 100, offset: int = 0
    ) -> list[ShareAccessLogEntity]:
        stmt = (
            select(ShareAccessLogORM)
            .where(ShareAccessLogORM.share_id == share_id)
            .order_by(ShareAccessLogORM.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_to_entity(orm) for orm in result.scalars().all()]

    async def recent_exists(
        self,
        *,
        share_id: int,
        session_id: str,
        action: str,
        since_ms: int,
    ) -> bool:
        """同会话短时去重：避免连点刷 VIEW_*。"""
        if not session_id:
            return False
        stmt = (
            select(ShareAccessLogORM.id)
            .where(
                ShareAccessLogORM.share_id == share_id,
                ShareAccessLogORM.session_id == session_id,
                ShareAccessLogORM.action == action,
                ShareAccessLogORM.created_at >= since_ms,
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None
