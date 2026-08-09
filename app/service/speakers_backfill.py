"""为已同步会议补齐 speakers_json，供参会人筛选。"""

from __future__ import annotations

import json
import logging

from sqlalchemy import or_, select

from app.data_model.entity.meeting_record import MeetingRecordUpdateEntity
from app.repository.orm.meeting_record import MeetingRecordORM
from app.repository.uow import UnitOfWork
from app.service.meeting_storage_service import MeetingStorageService
from app.service.transcript_anchor import extract_unique_speakers

logger = logging.getLogger(__name__)


async def backfill_missing_speakers(*, limit: int = 200) -> int:
    """扫描缺 speakers 的 COMPLETED 记录，从本地转写解析并回写。"""
    storage = MeetingStorageService()
    updated = 0
    async with UnitOfWork() as uow:
        assert uow._session is not None
        assert uow.meeting_records is not None
        rows = (
            await uow._session.execute(
                select(MeetingRecordORM)
                .where(
                    MeetingRecordORM.status == "COMPLETED",
                    MeetingRecordORM.owner_user_id.is_not(None),
                    MeetingRecordORM.minute_token.is_not(None),
                    or_(
                        MeetingRecordORM.speakers_json.is_(None),
                        MeetingRecordORM.speakers_json == "",
                    ),
                )
                .order_by(MeetingRecordORM.id.desc())
                .limit(limit)
            )
        ).scalars().all()
        for orm in rows:
            owner_id = orm.owner_user_id
            token = orm.minute_token
            if owner_id is None or not token:
                continue
            # 仅本地转写：避免启动时为缺字段会议批量打 R2
            text = storage._read_transcript_local(token, owner_user_id=int(owner_id))
            if text is None:
                continue
            speakers = extract_unique_speakers(text)
            # 即使为空也写入 []，避免每次启动重复扫同一批会议
            await uow.meeting_records.update(
                MeetingRecordUpdateEntity(
                    id=orm.id,
                    speakers_json=json.dumps(speakers, ensure_ascii=False),
                )
            )
            updated += 1
        if updated:
            await uow.commit()
    if updated:
        logger.info(
            "已为 %s 场会议回填参会人名单；此前列表人员筛选对这些会议无效",
            updated,
        )
    return updated
