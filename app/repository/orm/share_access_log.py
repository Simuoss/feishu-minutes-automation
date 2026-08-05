from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class ShareAccessLogORM(Base):
    __tablename__ = "share_access_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    access_key_id: Mapped[int] = mapped_column(Integer, index=True)
    share_id: Mapped[int] = mapped_column(Integer, index=True)
    minute_token: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(32))
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)
