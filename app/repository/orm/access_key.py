from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class AccessKeyORM(Base):
    __tablename__ = "access_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[int] = mapped_column(BigInteger)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
