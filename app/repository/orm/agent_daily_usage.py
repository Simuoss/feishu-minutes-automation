from sqlalchemy import BigInteger, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class AgentDailyUsageORM(Base):
    """按主体分日计数。不限额的主体也照样计，方便事后看用量。"""

    __tablename__ = "agent_daily_usage"
    __table_args__ = (
        UniqueConstraint(
            "subject_type", "subject_key", "day", name="uq_agent_usage_subject_day"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_type: Mapped[str] = mapped_column(String(16), index=True)
    subject_key: Mapped[str] = mapped_column(String(128), index=True)
    # 本地日期 YYYY-MM-DD（按 Asia/Shanghai 切日）
    day: Mapped[str] = mapped_column(String(10), index=True)
    used: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[int] = mapped_column(BigInteger)
