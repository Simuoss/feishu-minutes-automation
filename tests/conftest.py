"""跨测试文件共用的夹具。"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.db_base import Base


@pytest.fixture
def _memory_db(monkeypatch: pytest.MonkeyPatch):
    """把 UnitOfWork 指到内存库，避免测试写进开发机的 sqlite。"""
    # 建表要靠 ORM 类先注册到 Base 上，所以这些 import 少不了
    from app.repository.orm import (  # noqa: F401
        meeting_record,
        pipeline_job,
        voiceprint,
    )

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async def create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create())
    monkeypatch.setattr(
        "app.repository.uow.async_session_factory",
        async_sessionmaker(engine, expire_on_commit=False),
    )
    yield
    asyncio.run(engine.dispose())
