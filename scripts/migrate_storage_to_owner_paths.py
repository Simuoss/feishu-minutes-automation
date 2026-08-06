"""将本地会议目录从 {root}/{token} 迁到 {root}/{owner}/{token}。

用法（先停服）:
  python -m scripts.migrate_storage_to_owner_paths

无双读、无兼容层：迁移完成后旧路径目录会被删除。
规则：
- 仅处理 DB 中 owner_user_id 非空的 meeting_records
- 若存在旧目录 {root}/{token}：复制到每个相关 owner 下，再删旧目录
- 更新该行 storage_root_relpath / storage_path
- owner 为空的行：报错列出，不静默跳过成功
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.core.database import init_db
from app.core.resource_key import storage_relpath
from app.data_model.entity.meeting_record import MeetingRecordUpdateEntity
from app.repository.uow import UnitOfWork
from app.service.meeting_storage_service import MeetingStorageService

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate_owner_paths")


def _is_token_dir_name(name: str) -> bool:
    # 飞书妙记 token 常见前缀；也接受历史任意非纯数字目录（纯数字是新布局的 owner 层）
    if not name or name.startswith("."):
        return False
    if name.isdigit():
        return False
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description="会议目录迁移到 owner 子路径")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要执行的操作，不写盘不改库",
    )
    args = parser.parse_args()

    await init_db()
    storage = MeetingStorageService()
    root = Path(settings.storage_root)
    if not root.is_dir():
        logger.error("存储根目录不存在：%s", root)
        return 1

    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        from app.data_model.entity.meeting_record import MeetingRecordQueryEntity

        records = await uow.meeting_records.query(MeetingRecordQueryEntity())

    null_owners = [
        r for r in records if r.minute_token and r.owner_user_id is None
    ]
    if null_owners:
        tokens = sorted({r.minute_token for r in null_owners if r.minute_token})
        logger.error(
            "存在 owner_user_id 为空的会议记录 %d 条（token 例：%s），拒绝迁移",
            len(null_owners),
            ", ".join(tokens[:20]),
        )
        return 1

    by_token: dict[str, set[int]] = defaultdict(set)
    rows_by_token: dict[str, list] = defaultdict(list)
    for r in records:
        if not r.minute_token or r.id is None or r.owner_user_id is None:
            continue
        by_token[r.minute_token].add(int(r.owner_user_id))
        rows_by_token[r.minute_token].append(r)

    copied = 0
    updated = 0
    removed_old = 0
    already = 0
    errors = 0

    for token, owners in sorted(by_token.items()):
        old_dir = root / token
        for owner_id in sorted(owners):
            new_dir = storage.get_meeting_dir(token, owner_user_id=owner_id)
            rel = storage_relpath(owner_id, token)
            if new_dir.is_dir():
                already += 1
            elif old_dir.is_dir():
                logger.info(
                    "%s复制 %s -> %s",
                    "[dry-run] " if args.dry_run else "",
                    old_dir,
                    new_dir,
                )
                if not args.dry_run:
                    new_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(old_dir, new_dir)
                copied += 1
            else:
                logger.warning(
                    "无旧目录也无新目录 token=%s owner=%s，仅回写 DB 路径字段",
                    token,
                    owner_id,
                )

            for row in rows_by_token[token]:
                if row.owner_user_id != owner_id or row.id is None:
                    continue
                if (
                    row.storage_root_relpath == rel
                    and row.storage_path == str(new_dir)
                ):
                    continue
                logger.info(
                    "%s更新 DB id=%s storage_root_relpath=%s",
                    "[dry-run] " if args.dry_run else "",
                    row.id,
                    rel,
                )
                if not args.dry_run:
                    async with UnitOfWork() as uow:
                        assert uow.meeting_records is not None
                        await uow.meeting_records.update(
                            MeetingRecordUpdateEntity(
                                id=row.id,
                                storage_root_relpath=rel,
                                storage_path=str(new_dir),
                            )
                        )
                        await uow.commit()
                updated += 1

        if old_dir.is_dir():
            # 仅当所有目标 owner 目录都已存在（或 dry-run）才删旧目录
            all_ready = all(
                storage.get_meeting_dir(token, owner_user_id=oid).is_dir()
                or args.dry_run
                for oid in owners
            )
            if all_ready:
                logger.info(
                    "%s删除旧目录 %s",
                    "[dry-run] " if args.dry_run else "",
                    old_dir,
                )
                if not args.dry_run:
                    shutil.rmtree(old_dir)
                removed_old += 1
            else:
                logger.error(
                    "旧目录保留：部分 owner 目标未就绪 token=%s owners=%s",
                    token,
                    sorted(owners),
                )
                errors += 1

    # 扫描仍残留的旧式 token 顶层目录（无 DB 记录）
    orphans = [
        p.name
        for p in root.iterdir()
        if p.is_dir() and _is_token_dir_name(p.name) and p.name not in by_token
    ]
    if orphans:
        logger.warning(
            "仍有无 DB 归属的旧式顶层目录 %d 个（未自动删除）：%s",
            len(orphans),
            ", ".join(orphans[:30]),
        )

    logger.info(
        "迁移结束 dry_run=%s copied=%d updated_rows=%d removed_old=%d already=%d errors=%d",
        args.dry_run,
        copied,
        updated,
        removed_old,
        already,
        errors,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
