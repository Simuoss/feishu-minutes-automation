from typing import Literal

from pydantic import BaseModel, Field


class CloudMeetingItemResponse(BaseModel):
    minute_token: str
    display_info: str | None = None
    title: str | None = None
    create_time: str | None = None
    duration_ms: int | None = None
    cover: str | None = None
    app_link: str | None = None
    description: str | None = None
    url: str | None = None
    owner_id: str | None = None
    note_id: int | None = None
    keywords: str | None = None
    owner_name: str | None = None
    duration_text: str | None = None
    is_local: bool = False
    local_status: str = "NONE"
    record_id: int | None = None
    summary_status: str = "NONE"
    speakers: list[str] = Field(default_factory=list)


class CloudMeetingListResponse(BaseModel):
    items: list[CloudMeetingItemResponse]
    total: int
    filtered_total: int | None = None
    has_more: bool
    page_token: str | None = None


class DownloadMeetingsRequest(BaseModel):
    minute_tokens: list[str] = Field(min_length=1)
    skip_if_completed: bool = True
    # 已本地同步的会议：按需重拉音视频 / 转写（二者都为 false 时仍遵循 skip_if_completed）
    redownload_media: bool = False
    redownload_transcript: bool = False


class DownloadResultItemResponse(BaseModel):
    minute_token: str
    record_id: int | None = None
    status: str
    error_message: str | None = None
    error_code: int | None = None
    needs_app_scope: bool = False
    scope_auth_url: str | None = None
    needs_user_login: bool = False


class DownloadMeetingsResponse(BaseModel):
    results: list[DownloadResultItemResponse]


class DownloadProgressResponse(BaseModel):
    minute_token: str
    owner_user_id: int | None = None
    percent: int
    stage: str
    status: str
    error_message: str | None = None


class ProgressBatchItem(BaseModel):
    minute_token: str
    owner_user_id: int | None = None


class DownloadProgressBatchRequest(BaseModel):
    items: list[ProgressBatchItem] = Field(min_length=1, max_length=100)


class DownloadProgressBatchResponse(BaseModel):
    items: list[DownloadProgressResponse]


class LocalMediaFileResponse(BaseModel):
    name: str
    kind: str
    url: str


class LocalMeetingDetailResponse(BaseModel):
    minute_token: str
    title: str | None = None
    duration_ms: int | None = None
    has_video: bool | None = None
    media_files: list[LocalMediaFileResponse] = []
    has_transcript: bool = False
    downloaded_at: str | None = None


class LocalTranscriptResponse(BaseModel):
    minute_token: str
    transcript: str = ""


class TranscriptSourceResponse(BaseModel):
    minute_token: str
    source: str
    # 覆盖率只对飞书那份有意义，自建转写与本地导入都为空
    coverage: float | None = None
    can_switch_to_asr: bool = False
    can_switch_to_feishu: bool = False
    job_id: int | None = None
    message: str | None = None


class SwitchTranscriptSourceRequest(BaseModel):
    source: Literal["asr", "feishu"]
