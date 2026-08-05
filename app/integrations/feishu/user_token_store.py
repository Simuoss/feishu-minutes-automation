import logging
from datetime import datetime, timezone
from typing import Any

from app.core.async_bridge import run_async
from app.data_model.entity.feishu_user_token import FeishuUserTokenUpsertEntity
from app.repository.uow import UnitOfWork
from app.service.metadata_db_service import get_admin_user_id

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _to_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num < 10_000_000_000:
        return int(num * 1000)
    return int(num)


def _to_seconds(value: int | None) -> float | None:
    if value is None:
        return None
    if value > 10_000_000_000:
        return value / 1000.0
    return float(value)


def _entity_to_client_dict(entity: Any) -> dict[str, Any]:
    return {
        "access_token": entity.access_token,
        "refresh_token": entity.refresh_token,
        "scope": entity.scope,
        "expires_at": _to_seconds(entity.expires_at),
        "refresh_expires_at": _to_seconds(entity.refresh_expires_at),
        "updated_at": datetime.fromtimestamp(
            (entity.updated_at / 1000.0)
            if entity.updated_at > 10_000_000_000
            else entity.updated_at,
            tz=timezone.utc,
        ).isoformat(),
    }


class FeishuUserTokenStore:
    """按 admin 用户读写 DB。"""

    async def _load_db(self) -> dict[str, Any] | None:
        user_id = await get_admin_user_id()
        if user_id is None:
            return None
        async with UnitOfWork() as uow:
            assert uow.feishu_user_tokens is not None
            entity = await uow.feishu_user_tokens.get_by_user_id(user_id)
            if entity is None or not (entity.access_token or entity.refresh_token):
                return None
            return _entity_to_client_dict(entity)

    async def _save_db(self, data: dict[str, Any]) -> None:
        user_id = await get_admin_user_id()
        if user_id is None:
            raise RuntimeError("admin 用户不存在，无法保存飞书 Token")
        async with UnitOfWork() as uow:
            assert uow.feishu_user_tokens is not None
            await uow.feishu_user_tokens.upsert(
                FeishuUserTokenUpsertEntity(
                    user_id=user_id,
                    access_token=data.get("access_token"),
                    refresh_token=data.get("refresh_token"),
                    expires_at=_to_ms(data.get("expires_at")),
                    refresh_expires_at=_to_ms(data.get("refresh_expires_at")),
                    scope=data.get("scope"),
                    updated_at=_now_ms(),
                )
            )
            await uow.commit()

    async def _clear_db(self) -> None:
        user_id = await get_admin_user_id()
        if user_id is None:
            return
        async with UnitOfWork() as uow:
            assert uow.feishu_user_tokens is not None
            await uow.feishu_user_tokens.delete_by_user_id(user_id)
            await uow.commit()

    def load(self) -> dict[str, Any] | None:
        try:
            return run_async(self._load_db())
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "从 DB 读取飞书 Token 失败，需重新授权后才能拉列表。err=%s",
                exc,
            )
            return None

    def save(self, data: dict[str, Any]) -> None:
        try:
            run_async(self._save_db(data))
        except Exception as exc:  # noqa: BLE001
            logger.error("飞书 Token 写 DB 失败，授权状态不会持久化。err=%s", exc)
            raise

    def clear(self) -> None:
        try:
            run_async(self._clear_db())
        except Exception as exc:  # noqa: BLE001
            logger.error("清除飞书 Token DB 行失败。err=%s", exc)
            raise

    def is_authorized(self) -> bool:
        data = self.load()
        if not data:
            return False
        expires_at = data.get("expires_at")
        if expires_at and datetime.now(timezone.utc).timestamp() < float(expires_at) - 60:
            return bool(data.get("access_token"))
        return bool(data.get("refresh_token") or data.get("access_token"))
