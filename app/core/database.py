from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db_base import Base

__all__ = ["Base", "async_session_factory", "engine", "get_session", "init_db"]

_SQLITE_ALTER_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "users": [
        ("display_name", "VARCHAR(128)"),
        ("display_name_set_at", "BIGINT"),
        ("feishu_open_id", "VARCHAR(128)"),
        ("feishu_union_id", "VARCHAR(128)"),
        ("feishu_user_id", "VARCHAR(128)"),
        ("feishu_name", "VARCHAR(128)"),
        ("feishu_en_name", "VARCHAR(128)"),
        ("feishu_avatar_url", "VARCHAR(512)"),
        ("feishu_tenant_key", "VARCHAR(128)"),
        ("feishu_email", "VARCHAR(256)"),
        ("feishu_mobile", "VARCHAR(64)"),
        ("feishu_profile_json", "TEXT"),
    ],
    "meeting_records": [
        ("owner_user_id", "INTEGER"),
        ("summary_status", "VARCHAR(32)"),
        ("media_relpath", "VARCHAR(512)"),
        ("transcript_relpath", "VARCHAR(512)"),
        ("downloaded_at", "BIGINT"),
        ("storage_root_relpath", "VARCHAR(512)"),
        ("speakers_json", "TEXT"),
        ("create_time", "VARCHAR(64)"),
        ("transcript_source", "VARCHAR(16)"),
        ("transcript_coverage", "FLOAT"),
    ],
    "shares": [
        ("owner_user_id", "INTEGER"),
    ],
    "access_keys": [
        ("owner_user_id", "INTEGER"),
    ],
    "summary_runs": [
        ("owner_user_id", "INTEGER"),
        ("scene", "VARCHAR(32)"),
        ("scene_reason", "TEXT"),
    ],
    "r2_sync_states": [
        ("owner_user_id", "INTEGER"),
    ],
    "figures": [
        ("owner_user_id", "INTEGER"),
    ],
    "redaction_figures": [
        ("owner_user_id", "INTEGER"),
    ],
    "pipeline_jobs": [
        ("owner_user_id", "INTEGER"),
    ],
    "share_access_logs": [
        ("session_id", "VARCHAR(64)"),
        ("referer", "VARCHAR(512)"),
        ("device_type", "VARCHAR(32)"),
        ("browser", "VARCHAR(64)"),
        ("os", "VARCHAR(64)"),
        ("started_at", "BIGINT"),
        ("ended_at", "BIGINT"),
        ("dwell_ms", "INTEGER"),
        ("video_progress_pct", "INTEGER"),
        ("result", "VARCHAR(32)"),
        ("fail_reason", "VARCHAR(64)"),
        ("detail_json", "TEXT"),
        ("meeting_title", "VARCHAR(512)"),
    ],
}


def _ensure_sqlite_dir() -> None:
    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.split("///", 1)[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir()
Path(settings.storage_root).mkdir(parents=True, exist_ok=True)

_is_sqlite = settings.database_url.startswith("sqlite")
engine = create_async_engine(
    settings.database_url,
    echo=settings.app_debug,
    connect_args={"timeout": 30} if _is_sqlite else {},
)

if _is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def _ensure_columns_sync(connection) -> None:
    """SQLite 无 Alembic：对已有表幂等补列。"""
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    for table, columns in _SQLITE_ALTER_COLUMNS.items():
        if table not in existing_tables:
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, col_type in columns:
            if name in existing:
                continue
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}"))


def _ensure_user_feishu_index_sync(connection) -> None:
    """幂等创建 open_id 唯一索引（旧库 ALTER 加列后不会自动带约束）。"""
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_feishu_open_id "
            "ON users(feishu_open_id) WHERE feishu_open_id IS NOT NULL"
        )
    )


def _ensure_share_access_logs_nullable_key_sync(connection) -> None:
    """旧表 access_key_id 为 NOT NULL：重建表以支持公开分享日志。"""
    inspector = inspect(connection)
    if "share_access_logs" not in set(inspector.get_table_names()):
        return
    cols = {c["name"]: c for c in inspector.get_columns("share_access_logs")}
    key_col = cols.get("access_key_id")
    if key_col is None:
        return
    if key_col.get("nullable", True):
        return
    connection.execute(
        text(
            """
            CREATE TABLE share_access_logs__new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                access_key_id INTEGER,
                share_id INTEGER NOT NULL,
                minute_token VARCHAR(32) NOT NULL,
                meeting_title VARCHAR(512),
                action VARCHAR(32) NOT NULL,
                ip VARCHAR(64),
                user_agent TEXT,
                created_at BIGINT NOT NULL,
                session_id VARCHAR(64),
                referer VARCHAR(512),
                device_type VARCHAR(32),
                browser VARCHAR(64),
                os VARCHAR(64),
                started_at BIGINT,
                ended_at BIGINT,
                dwell_ms INTEGER,
                video_progress_pct INTEGER,
                result VARCHAR(32),
                fail_reason VARCHAR(64),
                detail_json TEXT
            )
            """
        )
    )
    # 按现有列动态拼 INSERT，兼容尚未 ALTER 完的旧库
    existing = set(cols)
    new_cols = [
        "id",
        "access_key_id",
        "share_id",
        "minute_token",
        "meeting_title",
        "action",
        "ip",
        "user_agent",
        "created_at",
        "session_id",
        "referer",
        "device_type",
        "browser",
        "os",
        "started_at",
        "ended_at",
        "dwell_ms",
        "video_progress_pct",
        "result",
        "fail_reason",
        "detail_json",
    ]
    select_exprs = [
        name if name in existing else "NULL" for name in new_cols
    ]
    connection.execute(
        text(
            f"INSERT INTO share_access_logs__new ({', '.join(new_cols)}) "
            f"SELECT {', '.join(select_exprs)} FROM share_access_logs"
        )
    )
    connection.execute(text("DROP TABLE share_access_logs"))
    connection.execute(
        text("ALTER TABLE share_access_logs__new RENAME TO share_access_logs")
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_share_access_logs_access_key_id "
            "ON share_access_logs (access_key_id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_share_access_logs_share_id "
            "ON share_access_logs (share_id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_share_access_logs_minute_token "
            "ON share_access_logs (minute_token)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_share_access_logs_created_at "
            "ON share_access_logs (created_at)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_share_access_logs_session_id "
            "ON share_access_logs (session_id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_share_access_logs_action "
            "ON share_access_logs (action)"
        )
    )


async def init_db() -> None:
    from app.repository.orm import (  # noqa: F401
        access_key,
        feishu_user_token,
        figure,
        invite_code,
        meeting_record,
        pipeline_job,
        r2_sync_state,
        recording_session,
        redaction_figure,
        share,
        share_access_log,
        summary_run,
        system_config,
        user,
        voiceprint,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.database_url.startswith("sqlite"):
            await conn.run_sync(_ensure_columns_sync)
            await conn.run_sync(_ensure_share_access_logs_nullable_key_sync)
            await conn.run_sync(_ensure_user_feishu_index_sync)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session
