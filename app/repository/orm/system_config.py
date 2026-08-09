from datetime import datetime

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class SystemConfigORM(Base):
    """系统业务配置：超管可在界面维护，密钥类仍留在 .env。"""

    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    value: Mapped[str] = mapped_column(Text, default="")
    remark: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[int] = mapped_column(BigInteger, default=0)
