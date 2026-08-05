"""对已同步到 R2 的会议，清理本地配图与原片视频。

用法:
  python scripts/purge_local_media_after_r2.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.core.database import init_db
from app.service.r2_media_service import r2_media_service


async def main() -> int:
    await init_db()
    if not r2_media_service.enabled():
        print("R2 未启用，跳过")
        return 1
    root = Path(settings.storage_root)
    if not root.is_dir():
        print("无会议目录")
        return 0
    total_assets = 0
    total_videos = 0
    for meeting_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        token = meeting_dir.name
        uploaded = await r2_media_service.sync_assets(token)
        purged_assets = r2_media_service.purge_local_assets_dir(token)
        purged_videos = 0
        if r2_media_service.video_ready(token):
            purged_videos = r2_media_service.purge_local_videos(token)
        else:
            # 尝试补传视频（有本地原片时）
            await r2_media_service.ensure_share_video(token)
            if r2_media_service.video_ready(token):
                purged_videos = r2_media_service.purge_local_videos(token)
        total_assets += uploaded + purged_assets
        total_videos += purged_videos
        print(
            f"{token} assets_upload={uploaded} assets_purge={purged_assets} "
            f"video_purge={purged_videos}"
        )
    print(f"done assets_ops={total_assets} video_purge={total_videos}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
