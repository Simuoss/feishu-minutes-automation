from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class AgentSessionORM(Base):
    """检索助手的一次对话。sdk_session_id 交给 Claude Agent SDK 续接上下文。"""

    __tablename__ = "agent_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sdk_session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # USER / ACCESS_KEY / ANON
    subject_type: Mapped[str] = mapped_column(String(16), index=True)
    subject_key: Mapped[str] = mapped_column(String(128), index=True)
    # 检索范围归属；scope=ALL（超管全站）时为 None
    owner_user_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    # OWN / ALL / SHARE
    scope: Mapped[str] = mapped_column(String(16), default="OWN")
    title: Mapped[str] = mapped_column(String(200), default="")
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)
