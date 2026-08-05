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
        ttl = expires_in if expires_in is not None else settings.r2_signed_url_ttl_seconds
        async with self._client() as client:
            url = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.r2_bucket.strip(), "Key": key},
                ExpiresIn=max(60, int(ttl)),
            )
        if not url:
            raise R2ClientError(f"生成签名 URL 失败 key={key}")
        return url


r2_client = R2Client()
