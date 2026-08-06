import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.integrations.feishu.user_token_store import FeishuUserTokenStore

logger = logging.getLogger(__name__)

TOKEN_INVALID_CODES = {99991663, 99991664}


def parse_scope_string(scope: str | None) -> set[str]:
    if not scope:
        return set()
    return {part.strip() for part in scope.split() if part.strip()}


def decode_jwt_scope(access_token: str | None) -> str | None:
    if not access_token:
        return None
    parts = access_token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = (-len(payload)) % 4
    if padding:
        payload += "=" * padding
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    scope = data.get("scope")
    return scope if isinstance(scope, str) else None


class UserAuthRequiredError(RuntimeError):
    """需要用户 OAuth 授权后才能调用仅支持 user_access_token 的 API。"""


class FeishuUserAuthClient:
    """绑定到具体 user_id 的飞书用户 OAuth / Token 客户端。"""

    def __init__(
        self,
        user_id: int,
        store: FeishuUserTokenStore | None = None,
    ) -> None:
        if user_id is None:
            raise ValueError("FeishuUserAuthClient 必须绑定 user_id")
        self._user_id = int(user_id)
        self._store = store or FeishuUserTokenStore()
        # 进程内短缓存：避免 async 请求里反复 run_async 开线程读 DB
        self._mem_access_token: str | None = None
        self._mem_expires_at: float = 0.0
        self._mem_scope: str | None = None
        self._mem_refresh_token: str | None = None
        self._mem_loaded: bool = False

    @property
    def user_id(self) -> int:
        return self._user_id

    def _cache_from_stored(self, stored: dict[str, Any] | None) -> None:
        self._mem_loaded = True
        if not stored:
            self._mem_access_token = None
            self._mem_expires_at = 0.0
            self._mem_scope = None
            self._mem_refresh_token = None
            return
        self._mem_access_token = stored.get("access_token")
        try:
            self._mem_expires_at = float(stored.get("expires_at") or 0)
        except (TypeError, ValueError):
            self._mem_expires_at = 0.0
        self._mem_scope = stored.get("scope")
        self._mem_refresh_token = stored.get("refresh_token")

    async def _load_stored(self) -> dict[str, Any] | None:
        stored = await self._store.load_async(self._user_id)
        self._cache_from_stored(stored)
        return stored

    def build_authorize_url(
        self,
        state: str = "feishu_minutes",
        *,
        redirect_uri: str | None = None,
        force_consent: bool = False,
    ) -> str:
        params = {
            "client_id": settings.feishu_app_id,
            "redirect_uri": redirect_uri or settings.feishu_oauth_redirect_uri,
            "response_type": "code",
            "state": state,
            "scope": settings.feishu_oauth_scopes,
        }
        if force_consent:
            params["prompt"] = "consent"
        return f"{settings.feishu_oauth_authorize_url}?{urlencode(params)}"

    def clear_authorization(self) -> None:
        self._store.clear(self._user_id)
        self._cache_from_stored(None)

    async def exchange_code(
        self, code: str, *, redirect_uri: str | None = None
    ) -> dict[str, Any]:
        body = {
            "grant_type": "authorization_code",
            "client_id": settings.feishu_app_id,
            "client_secret": settings.feishu_app_secret,
            "code": code,
            "redirect_uri": redirect_uri or settings.feishu_oauth_redirect_uri,
        }
        data = await self._post_token(body)
        self._persist_token_response(data)
        return data

    async def refresh_access_token(self) -> dict[str, Any]:
        stored = await self._load_stored()
        if not stored or not stored.get("refresh_token"):
            raise UserAuthRequiredError("缺少 refresh_token，请重新登录飞书授权")
        body = {
            "grant_type": "refresh_token",
            "client_id": settings.feishu_app_id,
            "client_secret": settings.feishu_app_secret,
            "refresh_token": stored["refresh_token"],
        }
        data = await self._post_token(body)
        self._persist_token_response(data)
        return data

    async def get_user_access_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            await self.refresh_access_token()
            if self._mem_access_token:
                return self._mem_access_token

        now = datetime.now(timezone.utc).timestamp()
        if (
            self._mem_access_token
            and self._mem_expires_at
            and now < self._mem_expires_at - 60
        ):
            return self._mem_access_token

        stored = await self._load_stored()
        if not stored:
            raise UserAuthRequiredError(
                "尚未完成飞书用户授权。搜索妙记列表必须使用 user_access_token，"
                "请访问 /api/v1/auth/feishu/login 完成登录。"
            )

        expires_at = stored.get("expires_at")
        token = stored.get("access_token")
        if token and expires_at and now < float(expires_at) - 60:
            return token

        if stored.get("refresh_token"):
            refreshed = await self.refresh_access_token()
            access_token = refreshed.get("access_token")
            if access_token:
                return access_token

        if token:
            return token

        raise UserAuthRequiredError("user_access_token 已失效，请重新登录飞书授权")

    def is_authorized(self) -> bool:
        return self._store.is_authorized(self._user_id)

    async def auth_status_async(self) -> tuple[bool, list[str], list[str]]:
        """一次异步读库，供 /auth/feishu/status，避免多次 run_async。"""
        stored = await self._load_stored()
        if not stored:
            return False, [], []
        expires_at = stored.get("expires_at")
        now = datetime.now(timezone.utc).timestamp()
        has_access = bool(stored.get("access_token")) and (
            not expires_at or now < float(expires_at) - 60
        )
        authorized = has_access or bool(stored.get("refresh_token") or stored.get("access_token"))
        if not authorized:
            return False, [], []
        if stored.get("scope"):
            granted = parse_scope_string(stored["scope"])
        else:
            granted = parse_scope_string(decode_jwt_scope(stored.get("access_token")))
        configured = parse_scope_string(settings.feishu_oauth_scopes)
        missing = sorted(
            scope
            for scope in configured
            if scope not in granted and scope != "offline_access"
        )
        return True, sorted(granted), missing

    def get_granted_scopes(self) -> set[str]:
        if self._mem_loaded:
            if self._mem_scope:
                return parse_scope_string(self._mem_scope)
            return parse_scope_string(decode_jwt_scope(self._mem_access_token))
        stored = self._store.load(self._user_id)
        self._cache_from_stored(stored)
        if not stored:
            return set()
        if stored.get("scope"):
            return parse_scope_string(stored["scope"])
        return parse_scope_string(decode_jwt_scope(stored.get("access_token")))

    def get_missing_scopes(self) -> list[str]:
        configured = parse_scope_string(settings.feishu_oauth_scopes)
        granted = self.get_granted_scopes()
        return sorted(
            scope
            for scope in configured
            if scope not in granted and scope != "offline_access"
        )

    async def _post_token(self, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                settings.feishu_oauth_token_url,
                json=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            try:
                payload = response.json()
            except ValueError as exc:
                logger.error(
                    "OAuth 响应非 JSON status=%s body=%s，怀疑网络或端点配置异常",
                    response.status_code,
                    response.text[:300],
                )
                raise RuntimeError(f"OAuth 响应异常 (HTTP {response.status_code})") from exc

        if payload.get("code") != 0:
            detail = (
                payload.get("error_description")
                or payload.get("msg")
                or payload.get("error")
            )
            logger.error(
                "换取 user_access_token 失败 code=%s detail=%s user_id=%s",
                payload.get("code"),
                detail,
                self._user_id,
            )
            raise RuntimeError(f"OAuth 失败 [{payload.get('code')}]: {detail}")

        if not payload.get("access_token"):
            raise RuntimeError("OAuth 响应缺少 access_token")
        return payload

    def _persist_token_response(self, data: dict[str, Any]) -> None:
        expires_in = int(data.get("expires_in", 7200))
        refresh_expires_in = data.get("refresh_token_expires_in") or data.get(
            "refresh_expires_in"
        )
        now = time.time()
        scope = data.get("scope") or decode_jwt_scope(data.get("access_token"))
        record = {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "scope": scope,
            "expires_at": now + expires_in,
            "refresh_expires_at": now + int(refresh_expires_in)
            if refresh_expires_in
            else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._store.save(self._user_id, record)
        self._cache_from_stored(record)
