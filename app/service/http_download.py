"""构造可被 latin-1 头安全承载的 Content-Disposition。"""

from urllib.parse import quote


def content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip() or "download"
    ascii_name = ascii_name.replace('"', "")
    encoded = quote(filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"
