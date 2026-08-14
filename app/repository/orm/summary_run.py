from sqlalchemy import BigInteger, Boolean, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class SummaryRunORM(Base):
    __tablename__ = "summary_runs"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "minute_token", name="uq_summary_runs_owner_token"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, index=True)
    minute_token: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elapsed_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    truncated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    transcript_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    anchor_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    anchor_aligned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    figure_planned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    figure_for_writing: Mapped[int | None] = mapped_column(Integer, nullable=True)
    figure_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    figure_scanned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    figure_sensitive: Mapped[int | None] = mapped_column(Integer, nullable=True)
    figure_redacted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    figure_abandoned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scene: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scene_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    redacted_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    r2_synced_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    r2_uploaded: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary_relpath: Mapped[str | None] = mapped_column(String(256), nullable=True)
    anchor_suspicious_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[int] = mapped_column(BigInteger)
