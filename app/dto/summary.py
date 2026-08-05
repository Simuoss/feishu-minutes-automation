from typing import Any, Literal

from pydantic import BaseModel, Field


SummaryMode = Literal["FULL", "REDACT", "WRITE", "R2_SYNC"]


class GenerateSummaryRequest(BaseModel):
    force: bool = False
    mode: SummaryMode = "FULL"


class GenerateSummaryResponse(BaseModel):
    minute_token: str
    status: str
    char_count: int = 0
    anchor_total: int = 0
    anchor_aligned: int = 0
    anchor_suspicious: int = 0
    figure_planned: int = 0
    figure_used: int = 0
    truncated: bool = False
    elapsed_seconds: float = 0.0
    error_message: str | None = None
    mode: str = "FULL"


class SummaryProgressResponse(BaseModel):
    minute_token: str
    status: str
    stage: str
    percent: int
    attempt: int = 0
    max_attempts: int = 0
    queue_position: int = 0
    error_message: str | None = None
    updated_at: str | None = None


class SummaryProgressBatchRequest(BaseModel):
    minute_tokens: list[str] = Field(min_length=1, max_length=100)


class SummaryProgressBatchResponse(BaseModel):
    items: list[SummaryProgressResponse]


class SummaryMetaData(BaseModel):
    generated_at: str | None = None
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
    anchor_suspicious: list[str] | int | None = None
    figure_planned: int | None = None
    figure_for_writing: int | None = None
    figure_used: int | None = None
    figure_scanned: int | None = None
    figure_sensitive: int | None = None
    figure_redacted: int | None = None
    figure_abandoned: int | None = None
    run_mode: str | None = None
    redacted_at: str | None = None
    r2_synced_at: str | None = None
    r2_uploaded: int | None = None


class SummaryDetailResponse(BaseModel):
    minute_token: str
    content: str
    meta: SummaryMetaData | None = None


class SummaryMetricsResponse(BaseModel):
    minute_token: str
    has_summary: bool
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    elapsed_seconds: float | None = None
    attempts: int | None = None
    truncated: bool | None = None
    transcript_chars: int | None = None
    summary_chars: int | None = None
    anchor_total: int | None = None
    anchor_aligned: int | None = None
    anchor_suspicious_count: int = 0
    figure_planned: int | None = None
    figure_for_writing: int | None = None
    figure_used: int | None = None
    figure_scanned: int | None = None
    figure_sensitive: int | None = None
    figure_redacted: int | None = None
    figure_abandoned: int | None = None
    generated_at: str | None = None
    run_mode: str | None = None
    r2_asset_count: int | None = None


class SummaryMetricsBatchRequest(BaseModel):
    minute_tokens: list[str] = Field(min_length=1, max_length=100)


class SummaryMetricsBatchResponse(BaseModel):
    items: list[SummaryMetricsResponse]


class RedactionRegionData(BaseModel):
    label: str = "敏感信息"
    x1: float
    y1: float
    x2: float
    y2: float


class RedactionAttemptData(BaseModel):
    attempt: int
    approved: bool | None = None
    reason: str = ""
    regions: list[RedactionRegionData] = Field(default_factory=list)


class RedactionFigureData(BaseModel):
    figure_id: str
    sensitive: bool = False
    status: str
    regions: list[RedactionRegionData] = Field(default_factory=list)
    attempts: list[RedactionAttemptData] = Field(default_factory=list)
    abandon_reason: str | None = None
    original_relative: str | None = None
    asset_relative: str | None = None
    original_url: str | None = None
    asset_url: str | None = None


class RedactionAuditResponse(BaseModel):
    minute_token: str
    saved_at: str | None = None
    scanned: int = 0
    sensitive: int = 0
    redacted: int = 0
    abandoned: int = 0
    figures: list[RedactionFigureData] = Field(default_factory=list)


class RedactionApproveRequest(BaseModel):
    figure_id: str
    regions: list[RedactionRegionData] = Field(min_length=1)


class RedactionAbandonRequest(BaseModel):
    figure_id: str
    reason: str = "人工放弃"


class RedactionActionResponse(BaseModel):
    minute_token: str
    figure_id: str
    status: str
    message: str = ""
    audit: RedactionAuditResponse | None = None
