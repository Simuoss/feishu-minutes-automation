import logging
from datetime import datetime, timezone
from typing import Any

from app.core.async_bridge import run_async
from app.data_model.entity.feishu_user_token import FeishuUserTokenUpsertEntity
from app.repository.uow import UnitOfWork

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
    """按显式 user_id 读写飞书 Token；禁止隐式回退或 ContextVar。"""

    @staticmethod
    def _require_user_id(user_id: int | None) -> int:
        if user_id is None:
            raise RuntimeError("缺少 user_id，拒绝读写飞书 Token")
        return int(user_id)

    async def load_async(self, user_id: int) -> dict[str, Any] | None:
        uid = self._require_user_id(user_id)
        async with UnitOfWork() as uow:
            assert uow.feishu_user_tokens is not None
            entity = await uow.feishu_user_tokens.get_by_user_id(uid)
            if entity is None or not (entity.access_token or entity.refresh_token):
                return None
            return _entity_to_client_dict(entity)

    async def save_async(self, user_id: int, data: dict[str, Any]) -> None:
        uid = self._require_user_id(user_id)
        async with UnitOfWork() as uow:
            assert uow.feishu_user_tokens is not None
            await uow.feishu_user_tokens.upsert(
                FeishuUserTokenUpsertEntity(
                    user_id=uid,
                    access_token=data.get("access_token"),
                    refresh_token=data.get("refresh_token"),
                    expires_at=_to_ms(data.get("expires_at")),
                    refresh_expires_at=_to_ms(data.get("refresh_expires_at")),
                    scope=data.get("scope"),
                    updated_at=_now_ms(),
                )
            )
            await uow.commit()

    async def clear_async(self, user_id: int) -> None:
        uid = self._require_user_id(user_id)
        async with UnitOfWork() as uow:
            assert uow.feishu_user_tokens is not None
            await uow.feishu_user_tokens.delete_by_user_id(uid)
            await uow.commit()

    def load(self, user_id: int) -> dict[str, Any] | None:
        try:
            return run_async(self.load_async(user_id))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "从 DB 读取飞书 Token 失败 user_id=%s，需重新授权后才能拉列表。err=%s",
                user_id,
                exc,
            )
            return None

    def save(self, user_id: int, data: dict[str, Any]) -> None:
        try:
            run_async(self.save_async(user_id, data))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "飞书 Token 写 DB 失败 user_id=%s，授权状态不会持久化。err=%s",
                user_id,
                exc,
            )
            raise

    def clear(self, user_id: int) -> None:
        try:
            run_async(self.clear_async(user_id))
        except Exception as exc:  # noqa: BLE001
            logger.error("清除飞书 Token DB 行失败 user_id=%s。err=%s", user_id, exc)
            raise

    def is_authorized(self, user_id: int) -> bool:
        data = self.load(user_id)
        if not data:
            return False
        expires_at = data.get("expires_at")
        if expires_at and datetime.now(timezone.utc).timestamp() < float(expires_at) - 60:
            return bool(data.get("access_token"))
        return bool(data.get("refresh_token") or data.get("access_token"))
