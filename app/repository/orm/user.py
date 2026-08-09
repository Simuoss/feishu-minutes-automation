from sqlalchemy import BigInteger, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class UserORM(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("feishu_open_id", name="uq_users_feishu_open_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 空字符串表示未设密码（飞书建号）；SQLite 旧库列可能仍为 NOT NULL
    password_hash: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(32), index=True, default="ACTIVE")
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name_set_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    feishu_open_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    feishu_union_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    feishu_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    feishu_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    feishu_en_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    feishu_avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    feishu_tenant_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    feishu_email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    feishu_mobile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feishu_profile_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)
