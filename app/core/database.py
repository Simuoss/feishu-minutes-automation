from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db_base import Base

__all__ = ["Base", "async_session_factory", "engine", "get_session", "init_db"]


def _ensure_sqlite_dir() -> None:
    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.split("///", 1)[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir()
Path(settings.storage_root).mkdir(parents=True, exist_ok=True)

engine = create_async_engine(
    settings.database_url,
    echo=settings.app_debug,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    from app.repository.orm import (  # noqa: F401
        access_key,
        meeting_record,
        recording_session,
        share,
        share_access_log,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session
