"""Cloudflare R2（S3 兼容）异步客户端。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aioboto3
from botocore.config import Config

from app.core.config import settings

logger = logging.getLogger(__name__)


class R2ClientError(RuntimeError):
    """R2 调用失败。"""


class R2Client:
    def __init__(self) -> None:
        self._session = aioboto3.Session()

    @property
    def configured(self) -> bool:
        return bool(
            settings.r2_enabled
            and (settings.r2_account_id or "").strip()
            and (settings.r2_access_key_id or "").strip()
            and (settings.r2_secret_access_key or "").strip()
            and (settings.r2_bucket or "").strip()
            and settings.r2_endpoint
        )

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        if not self.configured:
            raise R2ClientError(
                "R2 未正确配置：需要 R2_ENABLED 与 ACCOUNT_ID/ACCESS_KEY/SECRET/BUCKET"
            )
        async with self._session.client(
            "s3",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=settings.r2_access_key_id.strip(),
            aws_secret_access_key=settings.r2_secret_access_key.strip(),
            region_name="auto",
            config=Config(signature_version="s3v4"),
        ) as client:
            yield client

    async def upload_file(
        self,
        local_path: Path,
        key: str,
        *,
        content_type: str | None = None,
    ) -> str | None:
        if not local_path.is_file():
            raise R2ClientError(f"待上传文件不存在: {local_path}")
        kwargs: dict[str, Any] = {
            "Filename": str(local_path),
            "Bucket": settings.r2_bucket.strip(),
            "Key": key,
        }
        if content_type:
            kwargs["ExtraArgs"] = {"ContentType": content_type}
        async with self._client() as client:
            await client.upload_file(**kwargs)
            head = await client.head_object(
                Bucket=settings.r2_bucket.strip(),
                Key=key,
            )
        etag = (head.get("ETag") or "").strip('"') or None
        logger.info("已上传到 R2 key=%s size=%s etag=%s", key, local_path.stat().st_size, etag)
        return etag

    async def upload_bytes(
        self,
        data: bytes,
        key: str,
        *,
        content_type: str | None = None,
    ) -> str | None:
        kwargs: dict[str, Any] = {
            "Bucket": settings.r2_bucket.strip(),
            "Key": key,
            "Body": data,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        async with self._client() as client:
            await client.put_object(**kwargs)
            head = await client.head_object(
                Bucket=settings.r2_bucket.strip(),
                Key=key,
            )
        etag = (head.get("ETag") or "").strip('"') or None
        logger.info("已上传字节到 R2 key=%s size=%s etag=%s", key, len(data), etag)
        return etag

    async def get_bytes(self, key: str) -> bytes:
        async with self._client() as client:
            response = await client.get_object(
                Bucket=settings.r2_bucket.strip(),
                Key=key,
            )
            body = response.get("Body")
            if body is None:
                raise R2ClientError(f"从 R2 读取对象无 Body key={key}")
            data = await body.read()
        if not isinstance(data, (bytes, bytearray)):
            raise R2ClientError(f"从 R2 读取对象 Body 类型异常 key={key}")
        logger.info("已从 R2 读取字节 key=%s size=%s", key, len(data))
        return bytes(data)

    async def download_file(self, key: str, local_path: Path) -> Path:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._client() as client:
            await client.download_file(
                Bucket=settings.r2_bucket.strip(),
                Key=key,
                Filename=str(local_path),
            )
        if not local_path.is_file():
            raise R2ClientError(f"从 R2 下载后本地文件不存在 key={key}")
        logger.info("已从 R2 下载 key=%s -> %s", key, local_path)
        return local_path

    async def exists(self, key: str) -> bool:
        try:
            async with self._client() as client:
                await client.head_object(Bucket=settings.r2_bucket.strip(), Key=key)
            return True
        except Exception as exc:  # noqa: BLE001 — botocore 异常类型较多，按不存在处理
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            # ClientError 404
            if getattr(exc, "response", None) and int(
                getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                or 0
            ) == 404:
                return False
            logger.warning(
                "探测 R2 对象失败 key=%s，将按不存在处理以免阻断分享回退本地: %s",
                key,
                exc,
            )
            return False

    async def presign_get(self, key: str, *, expires_in: int | None = None) -> str:
        from app.core import runtime_config

        ttl = (
            expires_in
            if expires_in is not None
            else runtime_config.get_int("R2_SIGNED_URL_TTL_SECONDS", 7200)
        )
        async with self._client() as client:
            url = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.r2_bucket.strip(), "Key": key},
                ExpiresIn=max(60, int(ttl)),
            )
        if not url:
            raise R2ClientError(f"生成签名 URL 失败 key={key}")
        return url

    async def ensure_browser_cors(self, allowed_origins: list[str]) -> None:
        """允许前端浏览器直连 fetch 签名 URL（GET/HEAD）。"""
        origins = [o.strip().rstrip("/") for o in allowed_origins if (o or "").strip()]
        if not origins:
            return
        # 去重保序
        seen: set[str] = set()
        unique: list[str] = []
        for origin in origins:
            if origin in seen:
                continue
            seen.add(origin)
            unique.append(origin)
        rule = {
            "AllowedOrigins": unique,
            "AllowedMethods": ["GET", "HEAD"],
            "AllowedHeaders": ["*"],
            "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
            "MaxAgeSeconds": 3600,
        }
        try:
            async with self._client() as client:
                await client.put_bucket_cors(
                    Bucket=settings.r2_bucket.strip(),
                    CORSConfiguration={"CORSRules": [rule]},
                )
            logger.info("已配置 R2 浏览器 CORS origins=%s", unique)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "配置 R2 CORS 失败，前端直连正文可能被浏览器拦截。"
                "请在 Cloudflare R2 桶上手动允许前端 Origin。err=%s",
                exc,
            )


r2_client = R2Client()
