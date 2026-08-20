from dataclasses import dataclass

from app.data_model.unset import UNSET, Maybe


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
    """没填的字段保持原样，显式填 None 才是置空。"""

    id: int
    status: Maybe[str] = UNSET
    stage: Maybe[str] = UNSET
    percent: Maybe[float] = UNSET
    attempt: Maybe[int] = UNSET
    max_attempts: Maybe[int] = UNSET
    error_message: Maybe[str] = UNSET
    started_at: Maybe[int] = UNSET
    finished_at: Maybe[int] = UNSET
    broker_updated_at: Maybe[int] = UNSET
    mode: Maybe[str] = UNSET
