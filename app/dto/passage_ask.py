from typing import Literal

from pydantic import BaseModel, Field


AskSourceKind = Literal["SUMMARY", "TRANSCRIPT"]


class PassageAskImageData(BaseModel):
    media_type: str = Field(description="如 image/jpeg、image/png")
    data_base64: str = Field(description="不含 data: 前缀的纯 base64")


class PassageAskHistoryItemData(BaseModel):
    role: Literal["user", "assistant"]
    content: str = ""
    images: list[PassageAskImageData] = Field(default_factory=list)


class PassageAskRequest(BaseModel):
    source_kind: AskSourceKind
    selected_text: str = Field(min_length=1, max_length=4000)
    history: list[PassageAskHistoryItemData] = Field(default_factory=list, max_length=40)
    question: str | None = Field(default=None, max_length=4000)
    images: list[PassageAskImageData] = Field(default_factory=list, max_length=3)


class PassageAskResponse(BaseModel):
    reply: str
    user_message: str
    truncated: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    thinking: str = ""
    has_thinking: bool = False
