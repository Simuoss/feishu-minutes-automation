from sqlalchemy import Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class FigureORM(Base):
    __tablename__ = "figures"
    __table_args__ = (
        UniqueConstraint("minute_token", "figure_id", name="uq_figures_token_figure"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    minute_token: Mapped[str] = mapped_column(String(32), index=True)
    figure_id: Mapped[str] = mapped_column(String(64))
    relative_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    timestamp: Mapped[str | None] = mapped_column(String(32), nullable=True)
    frame_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    expect: Mapped[str | None] = mapped_column(Text, nullable=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="READY")
