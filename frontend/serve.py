"""前端静态资源服务（独立端口），并同域反代 /api → 后端。"""

from __future__ import annotations

import argparse
import http.client
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

FRONTEND_DIR = Path(__file__).resolve().parent
DEFAULT_API_UPSTREAM = os.environ.get("API_UPSTREAM", "http://127.0.0.1:7354")


class FrontendHandler(SimpleHTTPRequestHandler):
    api_upstream: str = DEFAULT_API_UPSTREAM

    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format: str, *args) -> None:
        print(f"[frontend] {self.address_string()} - {format % args}")

    def end_headers(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/assets/"):
            # 资源 URL 带 ?v= 版本；字体/图标也适合长缓存
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        elif path.endswith(".html") or path in {"", "/"}:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self) -> None:
        if self._is_api():
            self._proxy()
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        if self._is_api():
            self._proxy()
            return
        super().do_HEAD()

    def do_POST(self) -> None:
        if self._is_api():
            self._proxy()
            return
        self.send_error(404, "Not Found")

    def do_PUT(self) -> None:
        if self._is_api():
            self._proxy()
            return
        self.send_error(404, "Not Found")

    def do_PATCH(self) -> None:
        if self._is_api():
            self._proxy()
            return
        self.send_error(404, "Not Found")

    def do_DELETE(self) -> None:
        if self._is_api():
            self._proxy()
            return
        self.send_error(404, "Not Found")

    def do_OPTIONS(self) -> None:
        if self._is_api():
            self._proxy()
            return
        self.send_error(404, "Not Found")

    def _is_api(self) -> bool:
        return urlsplit(self.path).path.startswith("/api/")

    def _proxy(self) -> None:
        upstream = urlsplit(self.api_upstream)
        host = upstream.hostname or "127.0.0.1"
        port = upstream.port or (443 if upstream.scheme == "https" else 80)
        timeout = 600

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None

        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            lk = key.lower()
            if lk in {"host", "content-length", "connection", "transfer-encoding"}:
                continue
            headers[key] = value
        headers["Host"] = f"{host}:{port}" if port not in {80, 443} else host
        # 便于后端识别经前端反代的请求
        headers["X-Forwarded-Host"] = self.headers.get("Host") or host
        headers["X-Forwarded-Proto"] = "http"

        try:
            if upstream.scheme == "https":
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    host, port, timeout=timeout
                )
            else:
                conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            self.send_error(502, f"API upstream unavailable: {exc}")
            return

        try:
            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                lk = key.lower()
                if lk in {"transfer-encoding", "connection"}:
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            # 注意：不要走本类 end_headers 的静态缓存逻辑；直接写完头
            SimpleHTTPRequestHandler.end_headers(self)
            if self.command != "HEAD":
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    try:
                        self.wfile.flush()
                    except OSError:
                        break
        finally:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书妙记前端静态服务（含 /api 反代）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7355)
    parser.add_argument(
        "--api-upstream",
        default=DEFAULT_API_UPSTREAM,
        help="后端地址，默认 http://127.0.0.1:7354",
    )
    args = parser.parse_args()

    FrontendHandler.api_upstream = args.api_upstream.rstrip("/")
    handler = partial(FrontendHandler, directory=str(FRONTEND_DIR))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"前端运行于 http://127.0.0.1:{args.port}")
    print(f"API 反代 /api/* → {FrontendHandler.api_upstream}")
    server.serve_forever()


if __name__ == "__main__":
    main()
