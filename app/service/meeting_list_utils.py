import re
from datetime import datetime, timezone


def parse_duration_text(text: str | None) -> int | None:
    if not text:
        return None
    hours = minutes = seconds = 0
    if match := re.search(r"(\d+)\s*小时", text):
        hours = int(match.group(1))
    if match := re.search(r"(\d+)\s*分", text):
        minutes = int(match.group(1))
    if match := re.search(r"(\d+)\s*秒", text):
        seconds = int(match.group(1))
    total_ms = (hours * 3600 + minutes * 60 + seconds) * 1000
    return total_ms if total_ms > 0 else None


def parse_create_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def create_time_sort_key(value: str | None) -> float:
    parsed = parse_create_time(value)
    if parsed is None:
        return 0.0
    return parsed.timestamp()


def fuzzy_match_keyword(keyword: str, *fields: str | None) -> bool:
    needle = keyword.strip().lower()
    if not needle:
        return True
    haystack = " ".join(f for f in fields if f).lower()
    return needle in haystack
