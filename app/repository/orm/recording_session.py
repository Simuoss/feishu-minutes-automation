from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class RecordingSessionORM(Base):
    __tablename__ = "recording_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    unique_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    minute_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    meeting_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
