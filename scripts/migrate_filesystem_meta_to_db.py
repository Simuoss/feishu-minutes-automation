"""将 data/meetings 下 JSON 元数据幂等回填到 SQLite。

用法（项目根目录）:
  python -m scripts.migrate_filesystem_meta_to_db --owner-user-id 1

必须显式指定归属用户；禁止默认落到任何「主账户」。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.core.database import init_db
from app.repository.uow import UnitOfWork
from app.service.metadata_db_service import (
    replace_figures_from_manifest,
    replace_redaction_from_audit,
    upsert_meeting_meta,
    upsert_r2_meta,
    upsert_summary_meta,
)

TOKEN_FILE = Path("./data/feishu_user_token.json")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate_meta")


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("读取 JSON 失败 path=%s：%s", path, exc)
        return None
    return data if isinstance(data, dict) else None


async def _migrate_token_file(owner_user_id: int) -> bool:
    data = _load_json(TOKEN_FILE)
    if not data:
        return False
    from datetime import datetime, timezone

    from app.data_model.entity.feishu_user_token import FeishuUserTokenUpsertEntity

    def _to_ms(value: Any) -> int | None:
        if value is None:
            return None
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        return int(num * 1000) if num < 10_000_000_000 else int(num)

    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    async with UnitOfWork() as uow:
        assert uow.feishu_user_tokens is not None
        await uow.feishu_user_tokens.upsert(
            FeishuUserTokenUpsertEntity(
                user_id=owner_user_id,
                access_token=data.get("access_token"),
                refresh_token=data.get("refresh_token"),
                expires_at=_to_ms(data.get("expires_at")),
                refresh_expires_at=_to_ms(data.get("refresh_expires_at")),
                scope=data.get("scope"),
                updated_at=now,
            )
        )
        await uow.commit()
    return True


async def _backfill_null_owners(owner_user_id: int) -> tuple[int, int]:
    async with UnitOfWork() as uow:
        assert uow.shares is not None
        assert uow.access_keys is not None
        shares = await uow.shares.backfill_null_owner(owner_user_id)
        keys = await uow.access_keys.backfill_null_owner(owner_user_id)
        await uow.commit()
    return shares, keys


async def migrate_one(meeting_dir: Path, *, owner_user_id: int) -> dict[str, bool]:
    token = meeting_dir.name
    flags = {
        "meta": False,
        "summary": False,
        "figures": False,
        "redaction": False,
        "r2": False,
    }
    meta = _load_json(meeting_dir / "meta.json")
    if meta is not None:
        meta.setdefault("minute_token", token)
        await upsert_meeting_meta(token, meta, owner_user_id=owner_user_id)
        flags["meta"] = True

    summary_meta = _load_json(meeting_dir / "output" / "summary.meta.json")
    if summary_meta is not None:
        await upsert_summary_meta(
            token, summary_meta, owner_user_id=owner_user_id
        )
        flags["summary"] = True

    manifest = _load_json(meeting_dir / "agent" / "figures.manifest.json")
    if manifest is not None:
        await replace_figures_from_manifest(
            token, manifest, owner_user_id=owner_user_id
        )
        flags["figures"] = True

    audit = _load_json(meeting_dir / "agent" / "redaction" / "audit.json")
    if audit is not None:
        await replace_redaction_from_audit(
            token, audit, owner_user_id=owner_user_id
        )
        flags["redaction"] = True

    r2_meta = _load_json(meeting_dir / "output" / "r2.meta.json")
    if r2_meta is not None:
        await upsert_r2_meta(token, r2_meta, owner_user_id=owner_user_id)
        flags["r2"] = True

    return flags


async def main() -> int:
    parser = argparse.ArgumentParser(description="文件系统元数据迁移到 DB")
    parser.add_argument(
        "--owner-user-id",
        type=int,
        required=True,
        help="迁移数据归属的用户 id（必填，禁止默认主账户）",
    )
    args = parser.parse_args()
    owner_user_id = int(args.owner_user_id)

    await init_db()
    async with UnitOfWork() as uow:
        assert uow.users is not None
        user = await uow.users.get_by_id(owner_user_id)
    if user is None:
        logger.error("用户 id=%s 不存在，拒绝迁移", owner_user_id)
        return 1

    root = Path(settings.storage_root)
    if not root.is_dir():
        logger.warning("存储目录不存在：%s", root)
        meetings: list[Path] = []
    else:
        meetings = sorted(p for p in root.iterdir() if p.is_dir())

    counts = {
        "meetings": 0,
        "meta": 0,
        "summary": 0,
        "figures": 0,
        "redaction": 0,
        "r2": 0,
        "failed": 0,
    }
    failed_tokens: list[str] = []

    for meeting_dir in meetings:
        token = meeting_dir.name
        try:
            flags = await migrate_one(meeting_dir, owner_user_id=owner_user_id)
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            failed_tokens.append(token)
            logger.error("迁移失败 token=%s：%s", token, exc)
            continue
        counts["meetings"] += 1
        for key, ok in flags.items():
            if ok:
                counts[key] += 1
        logger.info(
            "已处理 %s meta=%s summary=%s figures=%s redaction=%s r2=%s",
            token,
            flags["meta"],
            flags["summary"],
            flags["figures"],
            flags["redaction"],
            flags["r2"],
        )

    token_ok = await _migrate_token_file(owner_user_id)
    share_n, key_n = await _backfill_null_owners(owner_user_id)

    logger.info(
        "迁移完成 owner=%s meetings=%d meta=%d summary=%d figures=%d redaction=%d r2=%d "
        "feishu_token=%s shares_owner=%d keys_owner=%d failed=%d",
        owner_user_id,
        counts["meetings"],
        counts["meta"],
        counts["summary"],
        counts["figures"],
        counts["redaction"],
        counts["r2"],
        token_ok,
        share_n,
        key_n,
        counts["failed"],
    )
    if failed_tokens:
        logger.warning("失败 token: %s", ", ".join(failed_tokens))
    return 0 if counts["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
