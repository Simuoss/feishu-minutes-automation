"""删除指定妙记本地/DB 状态，并模拟 minutes.minute.generated_v1 重新触发下载。

用法:
  .venv/Scripts/python.exe scripts/_e2e_replay_minute.py --token <minute_token> --owner 1
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import time
import uuid
from pathlib import Path

from sqlalchemy import text

from app.core.config import settings
from app.integrations.feishu.events import EVENT_MINUTE_GENERATED
from app.integrations.r2_client import r2_client
from app.repository.uow import UnitOfWork
from app.service.feishu_event_service import FeishuEventService
from app.service.meeting_storage_service import MeetingStorageService
from app.service.r2_media_service import r2_media_service


async def _delete_r2_prefix(prefix: str) -> int:
    if not r2_client.configured:
        return 0
    deleted = 0
    async with r2_client._client() as client:  # noqa: SLF001 — e2e 清理脚本
        token: str | None = None
        while True:
            kwargs = {
                "Bucket": settings.r2_bucket.strip(),
                "Prefix": prefix,
                "MaxKeys": 1000,
            }
            if token:
                kwargs["ContinuationToken"] = token
            resp = await client.list_objects_v2(**kwargs)
            contents = resp.get("Contents") or []
            keys = [{"Key": item["Key"]} for item in contents if item.get("Key")]
            if keys:
                await client.delete_objects(
                    Bucket=settings.r2_bucket.strip(),
                    Delete={"Objects": keys, "Quiet": True},
                )
                deleted += len(keys)
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
    return deleted


async def purge(minute_token: str, owner_user_id: int) -> None:
    storage = MeetingStorageService()
    meeting_dir = storage.get_meeting_dir(
        minute_token, owner_user_id=owner_user_id
    )
    if meeting_dir.is_dir():
        shutil.rmtree(meeting_dir, ignore_errors=True)
        print(f"[purge] removed local dir: {meeting_dir}")
    else:
        print(f"[purge] local dir absent: {meeting_dir}")

    async with UnitOfWork() as uow:
        session = uow._session  # noqa: SLF001
        share_ids = (
            await session.execute(
                text(
                    "SELECT id FROM shares WHERE owner_user_id=:o AND minute_token=:t"
                ),
                {"o": owner_user_id, "t": minute_token},
            )
        ).scalars().all()
        log_deleted = 0
        for sid in share_ids:
            result = await session.execute(
                text("DELETE FROM share_access_logs WHERE share_id=:id"),
                {"id": sid},
            )
            log_deleted += result.rowcount or 0
        if share_ids:
            print(
                f"[purge] share_access_logs: deleted={log_deleted} shares={len(share_ids)}"
            )

        tables = [
            ("figures", "DELETE FROM figures WHERE owner_user_id=:o AND minute_token=:t"),
            (
                "redaction_figures",
                "DELETE FROM redaction_figures WHERE owner_user_id=:o AND minute_token=:t",
            ),
            (
                "summary_runs",
                "DELETE FROM summary_runs WHERE owner_user_id=:o AND minute_token=:t",
            ),
            (
                "r2_sync_states",
                "DELETE FROM r2_sync_states WHERE owner_user_id=:o AND minute_token=:t",
            ),
            (
                "pipeline_jobs",
                "DELETE FROM pipeline_jobs WHERE owner_user_id=:o AND minute_token=:t",
            ),
            (
                "shares",
                "DELETE FROM shares WHERE owner_user_id=:o AND minute_token=:t",
            ),
            (
                "meeting_records",
                "DELETE FROM meeting_records WHERE owner_user_id=:o AND minute_token=:t",
            ),
        ]
        for name, sql in tables:
            result = await session.execute(
                text(sql), {"o": owner_user_id, "t": minute_token}
            )
            print(f"[purge] {name}: deleted={result.rowcount}")
        await uow.commit()

    prefix = f"meetings/{owner_user_id}/{minute_token}/"
    legacy = f"meetings/{minute_token}/"
    n1 = await _delete_r2_prefix(prefix)
    n2 = await _delete_r2_prefix(legacy)
    print(f"[purge] r2 objects deleted: owned={n1} legacy={n2}")


async def simulate_event(minute_token: str, owner_user_id: int) -> None:
    async with UnitOfWork() as uow:
        assert uow.users is not None
        user = await uow.users.get_by_id(owner_user_id)
        if user is None or not user.feishu_open_id:
            raise RuntimeError(
                f"owner={owner_user_id} 无 feishu_open_id，无法模拟 subscriber 事件"
            )
        open_id = user.feishu_open_id

    event_id = f"e2e-sim-{uuid.uuid4().hex[:16]}"
    payload = {
        "header": {
            "event_id": event_id,
            "event_type": EVENT_MINUTE_GENERATED,
        },
        "event": {
            "minute_token": minute_token,
            "subscriber_ids": [{"open_id": open_id}],
            "minute_source": {
                "source_type": "meeting",
                "source_entity_id": f"e2e-{minute_token}",
            },
        },
    }
    print(f"[sim] event_id={event_id} open_id={open_id}")
    result = await FeishuEventService().handle_event(payload)
    print(f"[sim] handle_event result={result}")


async def wait_ready(
    minute_token: str,
    owner_user_id: int,
    *,
    timeout_s: int,
    until: str = "video",
) -> None:
    """until=video：下载完成且分享片 READY；until=summary：再等到纪要终态。"""
    storage = MeetingStorageService()
    started = time.time()
    video_ready_logged = False
    while time.time() - started < timeout_s:
        async with UnitOfWork() as uow:
            session = uow._session  # noqa: SLF001
            row = (
                await session.execute(
                    text(
                        """
                        SELECT status, summary_status, has_video, title, error_message
                        FROM meeting_records
                        WHERE owner_user_id=:o AND minute_token=:t
                        ORDER BY id DESC LIMIT 1
                        """
                    ),
                    {"o": owner_user_id, "t": minute_token},
                )
            ).mappings().first()
        video = await r2_media_service.read_meta_async(
            minute_token, owner_user_id=owner_user_id
        )
        video_meta = (video or {}).get("video") or {}
        local = storage.find_video_path(
            minute_token, owner_user_id=owner_user_id
        )
        local_mb = (
            round(local.stat().st_size / 1024 / 1024, 1)
            if local and local.is_file()
            else None
        )
        print(
            f"[wait] elapsed={int(time.time()-started)}s "
            f"record={dict(row) if row else None} "
            f"video_status={video_meta.get('status')} "
            f"local_mb={local_mb}"
        )
        if row and row["status"] == "FAILED":
            raise RuntimeError(f"下载失败: {dict(row)}")
        download_ok = bool(row and row["status"] == "COMPLETED")
        video_ok = video_meta.get("status") == "READY"
        if download_ok and video_ok and not video_ready_logged:
            signed = await r2_media_service.presign_share_video(
                minute_token, owner_user_id=owner_user_id
            )
            print(f"[ready] share video READY signed={(signed or '')[:100]}")
            video_ready_logged = True
            if until == "video":
                return
        if until == "summary" and download_ok and video_ok:
            summary_status = (row or {}).get("summary_status")
            if summary_status in {"COMPLETED", "FAILED"}:
                print(f"[ready] summary terminal status={summary_status}")
                return
        if video_meta.get("status") == "FAILED" and download_ok:
            raise RuntimeError(f"分享上传失败: {video_meta}")
        await asyncio.sleep(10)
    raise TimeoutError(
        f"{timeout_s}s 内未达到 until={until}（download+share"
        f"{'+summary' if until == 'summary' else ''}）"
    )


async def trigger_via_http(minute_token: str, owner_user_id: int) -> None:
    """经运行中的后端触发下载，使压缩/纪要任务挂在 uvicorn 进程内。"""
    import httpx

    from app.core.jwt_auth import issue_user_token

    async with UnitOfWork() as uow:
        assert uow.users is not None
        user = await uow.users.get_by_id(owner_user_id)
        if user is None:
            raise RuntimeError(f"用户不存在 owner={owner_user_id}")
        username = user.username or f"u{owner_user_id}"
    jwt = issue_user_token(user_id=owner_user_id, username=username)
    base = f"http://127.0.0.1:{settings.app_port}/api/v1"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{base}/meetings/download",
            headers={"Authorization": f"Bearer {jwt}"},
            json={
                "minute_tokens": [minute_token],
                "skip_if_completed": False,
                "redownload_media": True,
                "redownload_transcript": True,
            },
        )
        print(f"[http] download -> {resp.status_code} {resp.text[:300]}")
        resp.raise_for_status()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True, help="飞书妙记 minute_token")
    parser.add_argument("--owner", type=int, required=True, help="本地 owner_user_id")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument(
        "--purge-only",
        action="store_true",
        help="只删除，不模拟事件（便于先重启后端再触发）",
    )
    parser.add_argument(
        "--trigger-only",
        action="store_true",
        help="只模拟事件，不删除",
    )
    parser.add_argument(
        "--via",
        choices=("event", "http"),
        default="event",
        help="event=进程内模拟飞书事件；http=打运行中后端的下载接口",
    )
    parser.add_argument(
        "--until",
        choices=("video", "summary"),
        default="video",
        help="等待终点：分享视频 READY，或再等到纪要终态",
    )
    args = parser.parse_args()

    if not args.trigger_only:
        await purge(args.token, args.owner)
    if args.purge_only:
        print("[done] purge-only")
        return
    if args.via == "http":
        await trigger_via_http(args.token, args.owner)
    else:
        await simulate_event(args.token, args.owner)
    await wait_ready(
        args.token,
        args.owner,
        timeout_s=args.timeout,
        until=args.until,
    )
    print(f"[done] e2e OK until={args.until}")


if __name__ == "__main__":
    asyncio.run(main())
