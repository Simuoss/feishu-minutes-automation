"""启动时为分享库补齐妙记 create_time，避免访客首次打开逐个打飞书。"""

from __future__ import annotations

import asyncio
import logging

from app.data_model.entity.share import ShareAdminQueryEntity
from app.repository.uow import UnitOfWork

logger = logging.getLogger(__name__)

FETCH_CONCURRENCY = 4


def pick_create_time_targets(
    pairs: list[tuple[int, str]],
    known: dict[tuple[int, str], str | None],
    *,
    limit: int = 200,
) -> list[tuple[int, str]]:
    """从分享 (owner, token) 里挑出尚未写入 create_time 的目标。"""
    seen: set[tuple[int, str]] = set()
    missing: list[tuple[int, str]] = []
    for owner, token in pairs:
        key = (int(owner), str(token).strip())
        if not key[1] or key in seen:
            continue
        seen.add(key)
        current = known.get(key)
        if current is not None and str(current).strip():
            continue
        missing.append(key)
        if len(missing) >= limit:
            break
    return missing


async def backfill_missing_create_times(*, limit: int = 200) -> int:
    """扫描有效分享，缺 create_time 的向飞书补拉并回写会议主档。"""
    pairs: list[tuple[int, str]] = []
    async with UnitOfWork() as uow:
        assert uow.shares is not None
        shares = await uow.shares.list_all(
            ShareAdminQueryEntity(status="ACTIVE", limit=2000, offset=0)
        )
        for share in shares:
            if share.owner_user_id is None or not share.minute_token:
                continue
            pairs.append((int(share.owner_user_id), share.minute_token.strip()))

    if not pairs:
        return 0

    known: dict[tuple[int, str], str | None] = {}
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        unique_pairs = list(dict.fromkeys(pairs))
        for owner, token in unique_pairs:
            record = await uow.meeting_records.get_latest_by_minute_token(
                token, owner_user_id=owner
            )
            if record is not None:
                known[(owner, token)] = record.create_time

    targets = pick_create_time_targets(pairs, known, limit=limit)
    if not targets:
        return 0

    semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)
    updated = 0

    async def _fill(owner: int, token: str) -> bool:
        from app.integrations.feishu.minutes_client import FeishuMinutesClient
        from app.service.meeting_storage_service import MeetingStorageService

        async with semaphore:
            try:
                info = await FeishuMinutesClient(user_id=owner).get_minute_info(
                    token, use_user_token=True
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "启动回填 create_time 失败 token=%s owner=%s：%s",
                    token,
                    owner,
                    exc,
                )
                return False
            create_time = str(info.create_time or "").strip()
            if not create_time:
                return False
            storage = MeetingStorageService()
            meta = await storage.read_meta_async(token, owner_user_id=owner) or {}
            meta["create_time"] = create_time
            await storage.write_meta_async(token, meta, owner_user_id=owner)
            return True

    results = await asyncio.gather(
        *(_fill(owner, token) for owner, token in targets),
        return_exceptions=True,
    )
    for item in results:
        if item is True:
            updated += 1
        elif isinstance(item, Exception):
            logger.warning("启动回填 create_time 任务异常：%s", item)
    if updated:
        logger.info(
            "已为 %s 场已分享会议回填 create_time；分享库不再需要首次逐个打飞书",
            updated,
        )
    return updated
