"""将附属表唯一键从裸 minute_token 迁到 (owner_user_id, minute_token)。

用法（先停服）:
  python -m scripts.migrate_aux_tables_owner_keys
  python -m scripts.migrate_aux_tables_owner_keys --dry-run
  python -m scripts.migrate_aux_tables_owner_keys --drop-orphans

规则（无双读、无兼容层）:
- 从 meeting_records 按 minute_token 展开 owner_user_id（多 owner 则复制行）
- 无法归属的行默认拒绝；加 --drop-orphans 时大声删除并继续
- 重建表以替换旧 UNIQUE(minute_token) / UNIQUE(token, figure_id)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import engine, init_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate_aux_owner_keys")

_TABLE_COLUMNS: dict[str, list[str]] = {
    "summary_runs": [
        "minute_token",
        "model",
        "input_tokens",
        "output_tokens",
        "attempts",
        "elapsed_seconds",
        "stop_reason",
        "truncated",
        "transcript_chars",
        "summary_chars",
        "anchor_total",
        "anchor_aligned",
        "figure_planned",
        "figure_for_writing",
        "figure_used",
        "figure_scanned",
        "figure_sensitive",
        "figure_redacted",
        "figure_abandoned",
        "run_mode",
        "generated_at",
        "redacted_at",
        "r2_synced_at",
        "r2_uploaded",
        "summary_relpath",
        "anchor_suspicious_json",
        "updated_at",
    ],
    "r2_sync_states": [
        "minute_token",
        "video_key",
        "video_etag",
        "video_status",
        "video_updated_at",
        "assets_json",
        "updated_at",
    ],
    "figures": [
        "minute_token",
        "figure_id",
        "relative_path",
        "timestamp",
        "frame_seconds",
        "expect",
        "purpose",
        "status",
    ],
    "redaction_figures": [
        "minute_token",
        "figure_id",
        "sensitive",
        "status",
        "abandon_reason",
        "original_relpath",
        "asset_relpath",
        "regions_json",
        "attempts_json",
    ],
    "pipeline_jobs": [
        "minute_token",
        "job_type",
        "mode",
        "status",
        "stage",
        "percent",
        "attempt",
        "max_attempts",
        "error_message",
        "started_at",
        "finished_at",
        "broker_updated_at",
    ],
}

_REBUILD_DDL: dict[str, str] = {
    "summary_runs": """
CREATE TABLE summary_runs_new (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    minute_token VARCHAR(32) NOT NULL,
    model VARCHAR(128),
    input_tokens INTEGER,
    output_tokens INTEGER,
    attempts INTEGER,
    elapsed_seconds FLOAT,
    stop_reason VARCHAR(64),
    truncated BOOLEAN,
    transcript_chars INTEGER,
    summary_chars INTEGER,
    anchor_total INTEGER,
    anchor_aligned INTEGER,
    figure_planned INTEGER,
    figure_for_writing INTEGER,
    figure_used INTEGER,
    figure_scanned INTEGER,
    figure_sensitive INTEGER,
    figure_redacted INTEGER,
    figure_abandoned INTEGER,
    run_mode VARCHAR(32),
    generated_at VARCHAR(64),
    redacted_at VARCHAR(64),
    r2_synced_at VARCHAR(64),
    r2_uploaded INTEGER,
    summary_relpath VARCHAR(256),
    anchor_suspicious_json TEXT,
    updated_at BIGINT NOT NULL,
    CONSTRAINT uq_summary_runs_owner_token UNIQUE (owner_user_id, minute_token)
)
""",
    "r2_sync_states": """
CREATE TABLE r2_sync_states_new (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    minute_token VARCHAR(32) NOT NULL,
    video_key VARCHAR(512),
    video_etag VARCHAR(128),
    video_status VARCHAR(32),
    video_updated_at VARCHAR(64),
    assets_json TEXT,
    updated_at BIGINT NOT NULL,
    CONSTRAINT uq_r2_sync_states_owner_token UNIQUE (owner_user_id, minute_token)
)
""",
    "figures": """
CREATE TABLE figures_new (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    minute_token VARCHAR(32) NOT NULL,
    figure_id VARCHAR(64) NOT NULL,
    relative_path VARCHAR(512),
    timestamp VARCHAR(32),
    frame_seconds FLOAT,
    expect TEXT,
    purpose TEXT,
    status VARCHAR(32) NOT NULL,
    CONSTRAINT uq_figures_owner_token_figure UNIQUE (owner_user_id, minute_token, figure_id)
)
""",
    "redaction_figures": """
CREATE TABLE redaction_figures_new (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    minute_token VARCHAR(32) NOT NULL,
    figure_id VARCHAR(64) NOT NULL,
    sensitive BOOLEAN NOT NULL,
    status VARCHAR(32) NOT NULL,
    abandon_reason TEXT,
    original_relpath VARCHAR(512),
    asset_relpath VARCHAR(512),
    regions_json TEXT,
    attempts_json TEXT,
    CONSTRAINT uq_redaction_figures_owner_token_figure UNIQUE (owner_user_id, minute_token, figure_id)
)
""",
    "pipeline_jobs": """
CREATE TABLE pipeline_jobs_new (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    minute_token VARCHAR(32) NOT NULL,
    job_type VARCHAR(32) NOT NULL,
    mode VARCHAR(32),
    status VARCHAR(32) NOT NULL,
    stage VARCHAR(64),
    percent FLOAT,
    attempt INTEGER,
    max_attempts INTEGER,
    error_message TEXT,
    started_at BIGINT,
    finished_at BIGINT,
    broker_updated_at BIGINT
)
""",
}

_REBUILD_INDEXES: dict[str, list[str]] = {
    "summary_runs": [
        "CREATE INDEX ix_summary_runs_owner_user_id ON summary_runs (owner_user_id)",
        "CREATE INDEX ix_summary_runs_minute_token ON summary_runs (minute_token)",
    ],
    "r2_sync_states": [
        "CREATE INDEX ix_r2_sync_states_owner_user_id ON r2_sync_states (owner_user_id)",
        "CREATE INDEX ix_r2_sync_states_minute_token ON r2_sync_states (minute_token)",
    ],
    "figures": [
        "CREATE INDEX ix_figures_owner_user_id ON figures (owner_user_id)",
        "CREATE INDEX ix_figures_minute_token ON figures (minute_token)",
        "CREATE INDEX ix_figures_status ON figures (status)",
    ],
    "redaction_figures": [
        "CREATE INDEX ix_redaction_figures_owner_user_id ON redaction_figures (owner_user_id)",
        "CREATE INDEX ix_redaction_figures_minute_token ON redaction_figures (minute_token)",
        "CREATE INDEX ix_redaction_figures_status ON redaction_figures (status)",
    ],
    "pipeline_jobs": [
        "CREATE INDEX ix_pipeline_jobs_owner_user_id ON pipeline_jobs (owner_user_id)",
        "CREATE INDEX ix_pipeline_jobs_minute_token ON pipeline_jobs (minute_token)",
        "CREATE INDEX ix_pipeline_jobs_job_type ON pipeline_jobs (job_type)",
        "CREATE INDEX ix_pipeline_jobs_status ON pipeline_jobs (status)",
    ],
}


async def _owners_by_token(conn) -> dict[str, list[int]]:
    rows = (
        await conn.execute(
            text(
                "SELECT minute_token, owner_user_id FROM meeting_records "
                "WHERE minute_token IS NOT NULL AND minute_token != '' "
                "AND owner_user_id IS NOT NULL"
            )
        )
    ).fetchall()
    mapping: dict[str, set[int]] = defaultdict(set)
    for token, owner in rows:
        mapping[str(token)].add(int(owner))
    return {token: sorted(owners) for token, owners in mapping.items()}


def _resolve_owners_for_row(
    token: str,
    current_owner: int | None,
    owners_by_token: dict[str, list[int]],
) -> list[int]:
    """决定该行应归属到哪些 owner。

    - 行已有 owner：保留该 owner；若 meeting 还有其他 owner，一并展开复制
    - 行无 owner：用 meeting_records 上该 token 的全部 owner
    """
    from_meetings = owners_by_token.get(token, [])
    if current_owner is not None:
        owners = {int(current_owner), *from_meetings}
        return sorted(owners)
    return list(from_meetings)


async def _migrate_table(
    conn,
    table: str,
    owners_by_token: dict[str, list[int]],
    *,
    dry_run: bool,
    drop_orphans: bool,
) -> None:
    table_sql = (
        await conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": table},
        )
    ).scalar()
    compact = " ".join((table_sql or "").split())
    migrated = (
        "owner_user_id INTEGER NOT NULL" in compact
        and (
            table == "pipeline_jobs"
            or ("uq_" in compact and "owner_user_id" in compact)
        )
    )
    if migrated:
        null_left = (
            await conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE owner_user_id IS NULL")
            )
        ).scalar()
        if not null_left:
            logger.info("%s 已是新结构，跳过", table)
            return

    cols = _TABLE_COLUMNS[table]
    col_list = ", ".join(cols)
    rows = (
        await conn.execute(
            text(f"SELECT id, owner_user_id, {col_list} FROM {table}")
        )
    ).mappings().all()

    expanded: list[dict] = []
    orphan_ids: list[int] = []
    orphan_tokens: set[str] = set()
    # summary/r2：按 (owner, token) 去重，保留最新 updated_at / 最大 id
    # figures/redaction：按 (owner, token, figure_id) 去重
    seen: dict[tuple, dict] = {}

    for row in rows:
        token = str(row["minute_token"])
        owners = _resolve_owners_for_row(
            token, row["owner_user_id"], owners_by_token
        )
        if not owners:
            orphan_ids.append(int(row["id"]))
            orphan_tokens.add(token)
            continue
        for owner in owners:
            payload = {c: row[c] for c in cols}
            payload["owner_user_id"] = owner
            if table in ("summary_runs", "r2_sync_states"):
                key = (owner, token)
            elif table in ("figures", "redaction_figures"):
                key = (owner, token, row["figure_id"])
            else:
                # pipeline_jobs：每条历史对每个 owner 各留一份（无唯一键）
                key = (owner, token, int(row["id"]))
            prev = seen.get(key)
            if prev is None:
                seen[key] = payload
                continue
            # 冲突时保留 updated_at 更大者，否则保留后写（更大 id 通常更新）
            prev_u = prev.get("updated_at") or 0
            cur_u = payload.get("updated_at") or 0
            if cur_u >= prev_u:
                seen[key] = payload

    expanded = list(seen.values())

    if orphan_ids:
        sample = ", ".join(sorted(orphan_tokens)[:20])
        logger.error(
            "%s 有 %d 行无法归属（token 例：%s）",
            table,
            len(orphan_ids),
            sample,
        )
        if not drop_orphans:
            raise RuntimeError(
                f"{table}: {len(orphan_ids)} 行无 owner，拒绝迁移。"
                "确认后可加 --drop-orphans 删除这些残留行。"
            )
        logger.warning(
            "%s 将丢弃孤儿行 %d 条（原 id 例：%s）",
            table,
            len(orphan_ids),
            ", ".join(str(i) for i in orphan_ids[:10]),
        )

    logger.info(
        "%s: 源行=%d → 展开=%d，丢弃孤儿=%d",
        table,
        len(rows),
        len(expanded),
        len(orphan_ids),
    )
    if dry_run:
        return

    await conn.execute(text(f"DROP TABLE IF EXISTS {table}_new"))
    await conn.execute(text(_REBUILD_DDL[table]))
    insert_cols = ["owner_user_id", *cols]
    placeholders = ", ".join(f":{c}" for c in insert_cols)
    insert_sql = text(
        f"INSERT INTO {table}_new ({', '.join(insert_cols)}) "
        f"VALUES ({placeholders})"
    )
    for payload in expanded:
        await conn.execute(insert_sql, payload)
    await conn.execute(text(f"DROP TABLE {table}"))
    await conn.execute(text(f"ALTER TABLE {table}_new RENAME TO {table}"))
    for idx_sql in _REBUILD_INDEXES[table]:
        await conn.execute(text(idx_sql))
    logger.info("已重建表 %s", table)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="附属表迁移到 (owner_user_id, minute_token) 唯一键"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--drop-orphans",
        action="store_true",
        help="删除无法归属到 meeting_records 的残留行（默认拒绝）",
    )
    args = parser.parse_args()

    await init_db()
    async with engine.begin() as conn:
        owners_by_token = await _owners_by_token(conn)
        logger.info("meeting_records 可归属 token=%d", len(owners_by_token))
        for table in _TABLE_COLUMNS:
            await _migrate_table(
                conn,
                table,
                owners_by_token,
                dry_run=args.dry_run,
                drop_orphans=args.drop_orphans,
            )

    if args.dry_run:
        logger.info("dry-run 完成，未写库")
    else:
        logger.info("附属表 owner 唯一键迁移完成")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except RuntimeError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
