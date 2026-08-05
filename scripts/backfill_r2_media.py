"""补全本地会议的 R2 配图与分享压缩视频。

用法（项目根目录）:
  .\\.venv\\Scripts\\python.exe scripts\\backfill_r2_media.py
  .\\.venv\\Scripts\\python.exe scripts\\backfill_r2_media.py --assets-only
  .\\.venv\\Scripts\\python.exe scripts\\backfill_r2_media.py --videos-only
  .\\.venv\\Scripts\\python.exe scripts\\backfill_r2_media.py --token obcnxxx
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.integrations.r2_client import r2_client
from app.service.meeting_storage_service import MeetingStorageService, VIDEO_EXTENSIONS
from app.service.r2_media_service import (
    VIDEO_STATUS_READY,
    r2_media_service,
)
from app.service.time_utils import utc_now_ms


async def _upload_precompressed_if_any(minute_token: str, storage: MeetingStorageService) -> bool:
    """若本地已有试压产物，直接上传，避免长视频再压一遍。"""
    candidate = (
        storage.get_meeting_dir(minute_token) / "agent" / "r2-compress-test" / "share.mp4"
    )
    if not candidate.is_file() or candidate.stat().st_size <= 0:
        return False
    if r2_media_service.video_ready(minute_token):
        print(f"  [video] skip ready token={minute_token}")
        return True

    key = r2_media_service.share_video_key(minute_token)
    print(f"  [video] reuse precompressed {candidate} -> {key}")
    etag = await r2_client.upload_file(candidate, key, content_type="video/mp4")
    meta = r2_media_service.read_meta(minute_token)
    meta["video"] = {
        "status": VIDEO_STATUS_READY,
        "key": key,
        "etag": etag,
        "updated_at": utc_now_ms(),
        "error": None,
        "source_name": candidate.name,
        "reused_precompressed": True,
    }
    r2_media_service.write_meta(minute_token, meta)
    return True


async def backfill_one(
    minute_token: str,
    *,
    do_assets: bool,
    do_videos: bool,
    storage: MeetingStorageService,
) -> None:
    print(f"== {minute_token} ==")
    if do_assets:
        n = await r2_media_service.sync_assets(minute_token)
        print(f"  [assets] uploaded={n}")
    if do_videos:
        if storage.find_video_path(minute_token) is None:
            print("  [video] no local video, skip")
            return
        if await _upload_precompressed_if_any(minute_token, storage):
            return
        if r2_media_service.video_ready(minute_token):
            print("  [video] already READY, skip")
            return
        print("  [video] compressing + uploading (may take a while)…")
        await r2_media_service.ensure_share_video(minute_token)
        status = (r2_media_service.read_meta(minute_token).get("video") or {}).get("status")
        print(f"  [video] status={status}")


def list_tokens(storage: MeetingStorageService, only: str | None) -> list[str]:
    if only:
        return [only.strip()]
    root = Path(storage._root)
    if not root.is_dir():
        return []
    tokens: list[str] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        media = d / "raw" / "media"
        assets = d / "output" / "assets"
        has_video = media.is_dir() and any(
            f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS for f in media.iterdir()
        )
        has_assets = assets.is_dir() and any(f.is_file() for f in assets.iterdir())
        if has_video or has_assets:
            tokens.append(d.name)
    return tokens


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill R2 assets/videos")
    parser.add_argument("--token", help="只处理指定 minute_token")
    parser.add_argument("--assets-only", action="store_true")
    parser.add_argument("--videos-only", action="store_true")
    args = parser.parse_args()

    if not r2_client.configured:
        print("R2 未配置或未启用，请检查 .env 中 R2_*")
        return 1

    do_assets = not args.videos_only
    do_videos = not args.assets_only
    storage = MeetingStorageService()
    tokens = list_tokens(storage, args.token)
    if not tokens:
        print("没有需要补传的会议")
        return 0

    from app.core.config import settings

    print(
        f"R2 bucket={settings.r2_bucket} meetings={len(tokens)} "
        f"assets={do_assets} videos={do_videos}"
    )
    for i, token in enumerate(tokens, 1):
        print(f"\n[{i}/{len(tokens)}]")
        try:
            await backfill_one(
                token,
                do_assets=do_assets,
                do_videos=do_videos,
                storage=storage,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR token={token}: {exc}")
    print("\nbackfill done")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
