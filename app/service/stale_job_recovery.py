"""进程启动时回收超时的进行中任务。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.repository.uow import UnitOfWork

logger = logging.getLogger(__name__)

DEFAULT_STALE_HOURS = 2.0


async def recover_stale_inflight_jobs(*, older_than_hours: float = DEFAULT_STALE_HOURS) -> None:
    """把超时仍卡在 DOWNLOADING / summary GENERATING 的记录标为 FAILED。

    进程崩溃或重启后内存池清空，这些行不会再有 worker 收尾；标失败后用户可重新提交。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    download_msg = (
        f"进程重启或任务超时（>{older_than_hours:g}h）仍处于 DOWNLOADING，已标记失败以便重试"
    )
    summary_msg = (
        f"进程重启或任务超时（>{older_than_hours:g}h）仍处于 GENERATING，已标记失败以便重试"
    )
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        download_failed, summary_failed = await uow.meeting_records.fail_stale_inflight(
            cutoff=cutoff,
            download_error=download_msg,
            summary_error=summary_msg,
        )
        if download_failed or summary_failed:
            await uow.commit()

    if download_failed:
        logger.warning(
            "启动回收：%d 条下载超时标为 FAILED（业务后果：列表显示同步失败，可重新下载）。样例 token=%s",
            len(download_failed),
            download_failed[0].minute_token,
        )
    if summary_failed:
        logger.warning(
            "启动回收：%d 条纪要生成超时标为 FAILED（业务后果：列表显示纪要失败，可重新生成）。样例 token=%s",
            len(summary_failed),
            summary_failed[0].minute_token,
        )
    if not download_failed and not summary_failed:
        logger.info(
            "启动回收：无超时 DOWNLOADING/GENERATING 记录（阈值 %gh）",
            older_than_hours,
        )
