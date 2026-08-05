from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class MeetingRecordORM(Base):
    __tablename__ = "meeting_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feishu_event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(128))
    unique_key: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    minute_token: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    meeting_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_video: Mapped[bool | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="PENDING")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
