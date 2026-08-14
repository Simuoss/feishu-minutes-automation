from dataclasses import dataclass


@dataclass
class PipelineJobEntity:
    id: int | None
    owner_user_id: int
    minute_token: str
    job_type: str
    mode: str | None
    status: str
    stage: str | None
    percent: float | None
    attempt: int | None
    max_attempts: int | None
    error_message: str | None
    started_at: int | None
    finished_at: int | None
    broker_updated_at: int | None


@dataclass
class PipelineJobCreateEntity:
    owner_user_id: int
    minute_token: str
    job_type: str
    mode: str | None = None
    status: str = "PENDING"
    stage: str | None = None
    percent: float | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    error_message: str | None = None
    started_at: int | None = None
    finished_at: int | None = None
    broker_updated_at: int | None = None


@dataclass
class PipelineJobUpdateEntity:
    id: int
    status: str | None = None
    stage: str | None = None
    percent: float | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    error_message: str | None = None
    started_at: int | None = None
    finished_at: int | None = None
    broker_updated_at: int | None = None
    mode: str | None = None
