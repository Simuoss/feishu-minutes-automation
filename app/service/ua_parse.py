"""粗粒度 User-Agent 解析：设备 / 浏览器 / OS，本地规则、无外部依赖。"""

from __future__ import annotations


def parse_user_agent(ua: str | None) -> dict[str, str | None]:
    raw = (ua or "").strip()
    if not raw:
        return {"device_type": None, "browser": None, "os": None}

    lower = raw.lower()
    device = "MOBILE" if _is_mobile(lower) else "DESKTOP"
    return {
        "device_type": device,
        "browser": _browser(lower),
        "os": _os(lower),
    }


def _is_mobile(lower: str) -> bool:
    needles = (
        "mobile",
        "android",
        "iphone",
        "ipod",
        "ipad",
        "iemobile",
        "opera mini",
        "opera mobi",
    )
    return any(n in lower for n in needles)


def _browser(lower: str) -> str:
    if "edg/" in lower or "edge/" in lower:
        return "Edge"
    if "opr/" in lower or "opera" in lower:
        return "Opera"
    if "chrome/" in lower and "chromium" not in lower:
        return "Chrome"
    if "firefox/" in lower or "fxios/" in lower:
        return "Firefox"
    if "safari/" in lower and "chrome/" not in lower:
        return "Safari"
    if "msie" in lower or "trident/" in lower:
        return "IE"
    return "Other"


def _os(lower: str) -> str:
    if "android" in lower:
        return "Android"
    if "iphone" in lower or "ipad" in lower or "ipod" in lower or "ios" in lower:
        return "iOS"
    if "windows" in lower:
        return "Windows"
    if "mac os" in lower or "macintosh" in lower:
        return "macOS"
    if "cros" in lower:
        return "ChromeOS"
    if "linux" in lower:
        return "Linux"
    return "Other"
