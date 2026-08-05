from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class FeishuUserTokenORM(Base):
    """本地单机暂明文存 token；后续可换加密列。"""

    __tablename__ = "feishu_user_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    refresh_expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    scope: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    updated_at: Mapped[int] = mapped_column(BigInteger)
