from sqlalchemy import BigInteger, Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class SummaryRunORM(Base):
    __tablename__ = "summary_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    minute_token: Mapped[str] = mapped_column(String(32), unique=True, index=True)
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
    generated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    redacted_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    r2_synced_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    r2_uploaded: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary_relpath: Mapped[str | None] = mapped_column(String(256), nullable=True)
    anchor_suspicious_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[int] = mapped_column(BigInteger)
