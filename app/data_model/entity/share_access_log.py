from dataclasses import dataclass


@dataclass
class ShareAccessLogEntity:
    id: int | None
    access_key_id: int | None
    share_id: int
    minute_token: str
    action: str
    ip: str | None
    user_agent: str | None
    created_at: int
    meeting_title: str | None = None
    session_id: str | None = None
    referer: str | None = None
    device_type: str | None = None
    browser: str | None = None
    os: str | None = None
    started_at: int | None = None
    ended_at: int | None = None
    dwell_ms: int | None = None
    video_progress_pct: int | None = None
    result: str | None = None
    fail_reason: str | None = None
    detail_json: str | None = None


@dataclass
class ShareAccessLogCreateEntity:
    share_id: int
    minute_token: str
    action: str
    created_at: int
    access_key_id: int | None = None
    meeting_title: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    session_id: str | None = None
    referer: str | None = None
    device_type: str | None = None
    browser: str | None = None
    os: str | None = None
    started_at: int | None = None
    ended_at: int | None = None
    dwell_ms: int | None = None
    video_progress_pct: int | None = None
    result: str | None = None
    fail_reason: str | None = None
    detail_json: str | None = None
