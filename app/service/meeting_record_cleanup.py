"""清理失败与重复的会议主档：每用户每 token 只留一条。"""

from __future__ import annotations

import logging
from collections import defaultdict

from app.repository.uow import UnitOfWork

logger = logging.getLogger(__name__)


def _keep_id(rows: list) -> int | None:
    """同 (owner, token) 组内保留策略：优先最新 COMPLETED，否则不保留（全失败则清空）。"""
    completed = [r for r in rows if (r.status or "").upper() == "COMPLETED" and r.id]
    if completed:
        return max(completed, key=lambda r: int(r.id)).id
    return None


async def cleanup_failed_and_duplicate_meetings() -> dict[str, int]:
    """删除 FAILED，以及同一用户同一 token 的旧重复行。"""
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        rows = await uow.meeting_records.list_all_with_token()
        groups: dict[tuple[int, str], list] = defaultdict(list)
        for row in rows:
            if row.owner_user_id is None or not row.minute_token or row.id is None:
                continue
            groups[(int(row.owner_user_id), str(row.minute_token))].append(row)

        delete_ids: list[int] = []
        kept = 0
        for _key, group in groups.items():
            keep = _keep_id(group)
            if keep is None:
                delete_ids.extend(int(r.id) for r in group if r.id is not None)
                continue
            kept += 1
            for r in group:
                if r.id is not None and int(r.id) != keep:
                    delete_ids.append(int(r.id))

        removed = await uow.meeting_records.delete_by_ids(delete_ids)
        await uow.commit()

    logger.info(
        "会议主档清理完成：保留 %s 场（唯一用户+token），删除 %s 行失败/重复旧记录",
        kept,
        removed,
    )
    return {"kept": kept, "deleted": removed, "groups": len(groups)}
