from dataclasses import dataclass, field
from typing import Any


@dataclass
class RedactionFigureEntity:
    id: int | None
    owner_user_id: int
    minute_token: str
    figure_id: str
    sensitive: bool
    status: str
    abandon_reason: str | None
    original_relpath: str | None
    asset_relpath: str | None
    regions: list[Any]
    attempts: list[Any]


@dataclass
class RedactionFigureUpsertEntity:
    owner_user_id: int
    minute_token: str
    figure_id: str
    sensitive: bool = False
    status: str = "CLEAN"
    abandon_reason: str | None = None
    original_relpath: str | None = None
    asset_relpath: str | None = None
    regions: list[Any] = field(default_factory=list)
    attempts: list[Any] = field(default_factory=list)
