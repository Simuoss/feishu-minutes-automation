from typing import Literal

from pydantic import BaseModel, Field


class ExportBatchItem(BaseModel):
    minute_token: str
    owner_user_id: int | None = None


class ExportBatchRequest(BaseModel):
    items: list[ExportBatchItem] = Field(min_length=1, max_length=50)
    include_summary: bool = True
    include_transcript: bool = True
    summary_format: Literal["pdf", "docx", "md"] = "md"
    transcript_format: Literal["md", "txt"] = "txt"
