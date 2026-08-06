from dataclasses import dataclass


@dataclass
class FigureEntity:
    id: int | None
    owner_user_id: int
    minute_token: str
    figure_id: str
    relative_path: str | None
    timestamp: str | None
    frame_seconds: float | None
    expect: str | None
    purpose: str | None
    status: str


@dataclass
class FigureUpsertEntity:
    owner_user_id: int
    minute_token: str
    figure_id: str
    relative_path: str | None = None
    timestamp: str | None = None
    frame_seconds: float | None = None
    expect: str | None = None
    purpose: str | None = None
    status: str = "READY"
