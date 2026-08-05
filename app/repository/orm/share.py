from sqlalchemy import BigInteger, Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class ShareORM(Base):
    __tablename__ = "shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    share_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    minute_token: Mapped[str] = mapped_column(String(32), index=True)
    access_mode: Mapped[str] = mapped_column(String(32), index=True)
    allow_export: Mapped[bool] = mapped_column(Boolean, default=False)
    access_key_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
