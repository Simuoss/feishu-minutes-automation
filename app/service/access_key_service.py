from __future__ import annotations

import hashlib
import logging
import secrets

from app.data_model.entity.access_key import AccessKeyCreateEntity, AccessKeyEntity, AccessKeyQueryEntity
from app.data_model.entity.share_access_log import ShareAccessLogEntity
from app.repository.uow import UnitOfWork
from app.service.time_utils import utc_now_ms

logger = logging.getLogger(__name__)


def hash_access_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_access_key_plaintext() -> str:
    return f"sk_{secrets.token_urlsafe(32)}"


class AccessKeyService:
    async def create(self, *, name: str, expires_at: int) -> tuple[AccessKeyEntity, str]:
        name = name.strip()
        if not name:
            raise ValueError("密钥名称不能为空")
        now = utc_now_ms()
        if expires_at <= now:
            raise ValueError("过期时间必须晚于当前时间")

        plaintext = generate_access_key_plaintext()
        entity = AccessKeyCreateEntity(
            name=name,
            key_hash=hash_access_key(plaintext),
            key_prefix=plaintext[:10],
            expires_at=expires_at,
            created_at=now,
        )
        async with UnitOfWork() as uow:
            assert uow.access_keys is not None
            created = await uow.access_keys.create(entity)
            await uow.commit()
        return created, plaintext

    async def list_keys(self, *, include_revoked: bool = False) -> list[AccessKeyEntity]:
        async with UnitOfWork() as uow:
            assert uow.access_keys is not None
            return await uow.access_keys.list_all(
                AccessKeyQueryEntity(include_revoked=include_revoked)
            )

    async def revoke(self, key_id: int) -> AccessKeyEntity:
        async with UnitOfWork() as uow:
            assert uow.access_keys is not None
            row = await uow.access_keys.get_by_id(key_id)
            if row is None:
                raise LookupError("密钥不存在")
            if row.revoked_at is not None:
                return row
            updated = await uow.access_keys.revoke(key_id, utc_now_ms())
            await uow.commit()
            if updated is None:
                raise LookupError("密钥不存在")
            return updated

    async def get(self, key_id: int) -> AccessKeyEntity | None:
        async with UnitOfWork() as uow:
            assert uow.access_keys is not None
            return await uow.access_keys.get_by_id(key_id)

    def is_usable(self, key: AccessKeyEntity, *, now: int | None = None) -> bool:
        now = now if now is not None else utc_now_ms()
        if key.revoked_at is not None:
            return False
        return key.expires_at > now

    async def list_logs(
        self, key_id: int, *, limit: int = 100, offset: int = 0
    ) -> list[ShareAccessLogEntity]:
        async with UnitOfWork() as uow:
            assert uow.access_keys is not None
            assert uow.share_access_logs is not None
            if await uow.access_keys.get_by_id(key_id) is None:
                raise LookupError("密钥不存在")
            return await uow.share_access_logs.list_by_key(key_id, limit=limit, offset=offset)


access_key_service = AccessKeyService()
