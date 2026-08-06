from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class InviteCodeORM(Base):
    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by_user_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    used_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="ACTIVE")
