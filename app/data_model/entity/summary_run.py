from dataclasses import dataclass, field
from typing import Any


@dataclass
class SummaryRunEntity:
    id: int | None
    owner_user_id: int
    minute_token: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    attempts: int | None
    elapsed_seconds: float | None
    stop_reason: str | None
    truncated: bool | None
    transcript_chars: int | None
    summary_chars: int | None
    anchor_total: int | None
    anchor_aligned: int | None
    figure_planned: int | None
    figure_for_writing: int | None
    figure_used: int | None
    figure_scanned: int | None
    figure_sensitive: int | None
    figure_redacted: int | None
    figure_abandoned: int | None
    run_mode: str | None
    scene: str | None
    scene_reason: str | None
    generated_at: str | None
    redacted_at: str | None
    r2_synced_at: str | None
    r2_uploaded: int | None
    summary_relpath: str | None
    anchor_suspicious: list[Any]
    updated_at: int


@dataclass
class SummaryRunUpsertEntity:
    owner_user_id: int
    minute_token: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    attempts: int | None = None
    elapsed_seconds: float | None = None
    stop_reason: str | None = None
    truncated: bool | None = None
    transcript_chars: int | None = None
    summary_chars: int | None = None
    anchor_total: int | None = None
    anchor_aligned: int | None = None
    figure_planned: int | None = None
    figure_for_writing: int | None = None
    figure_used: int | None = None
    figure_scanned: int | None = None
    figure_sensitive: int | None = None
    figure_redacted: int | None = None
    figure_abandoned: int | None = None
    run_mode: str | None = None
    scene: str | None = None
    scene_reason: str | None = None
    generated_at: str | None = None
    redacted_at: str | None = None
    r2_synced_at: str | None = None
    r2_uploaded: int | None = None
    summary_relpath: str | None = "output/summary.md"
    anchor_suspicious: list[Any] = field(default_factory=list)
    updated_at: int = 0
