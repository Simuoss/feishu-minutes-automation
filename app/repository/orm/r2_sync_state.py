from sqlalchemy import BigInteger, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class R2SyncStateORM(Base):
    __tablename__ = "r2_sync_states"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "minute_token", name="uq_r2_sync_states_owner_token"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, index=True)
    minute_token: Mapped[str] = mapped_column(String(32), index=True)
    video_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    video_etag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    video_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    video_updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assets_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[int] = mapped_column(BigInteger)
