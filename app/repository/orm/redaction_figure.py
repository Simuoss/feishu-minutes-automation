from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class RedactionFigureORM(Base):
    __tablename__ = "redaction_figures"
    __table_args__ = (
        UniqueConstraint(
            "minute_token", "figure_id", name="uq_redaction_figures_token_figure"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    minute_token: Mapped[str] = mapped_column(String(32), index=True)
    figure_id: Mapped[str] = mapped_column(String(64))
    sensitive: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="CLEAN")
    abandon_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_relpath: Mapped[str | None] = mapped_column(String(512), nullable=True)
    asset_relpath: Mapped[str | None] = mapped_column(String(512), nullable=True)
    regions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
