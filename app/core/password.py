"""口令哈希（stdlib pbkdf2），供 users 表使用。"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ITERATIONS = 260_000
_SCHEME = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        _ITERATIONS,
    ).hex()
    return f"{_SCHEME}${_ITERATIONS}${salt}${digest}"


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash or not password:
        return False
    parts = password_hash.split("$")
    if len(parts) != 4 or parts[0] != _SCHEME:
        return False
    try:
        iterations = int(parts[1])
    except ValueError:
        return False
    salt = parts[2]
    expected = parts[3]
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(digest, expected)
