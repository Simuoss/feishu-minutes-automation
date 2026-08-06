from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db_base import Base

__all__ = ["Base", "async_session_factory", "engine", "get_session", "init_db"]

_SQLITE_ALTER_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "meeting_records": [
        ("owner_user_id", "INTEGER"),
        ("summary_status", "VARCHAR(32)"),
        ("media_relpath", "VARCHAR(512)"),
        ("transcript_relpath", "VARCHAR(512)"),
        ("downloaded_at", "BIGINT"),
        ("storage_root_relpath", "VARCHAR(512)"),
    ],
    "shares": [
        ("owner_user_id", "INTEGER"),
    ],
    "access_keys": [
        ("owner_user_id", "INTEGER"),
    ],
    "summary_runs": [
        ("owner_user_id", "INTEGER"),
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
        user,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.database_url.startswith("sqlite"):
            await conn.run_sync(_ensure_columns_sync)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session
