"""取客户端真实 IP。

线上是 Cloudflare Tunnel 进来的，request.client.host 永远是隧道那一端，
所以先看 Cloudflare 的头，再退回通用的 X-Forwarded-For。
"""

from __future__ import annotations

from fastapi import Request


def client_ip(request: Request) -> str:
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        # 最左边那个是原始客户端，右边都是经过的代理
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    return request.client.host if request.client else "unknown"
