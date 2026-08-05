import logging
import time
from typing import Any

import httpx

from app.core.config import settings
from app.integrations.feishu.user_auth import TOKEN_INVALID_CODES, FeishuUserAuthClient

logger = logging.getLogger(__name__)


class FeishuAuthClient:
    """飞书 tenant_access_token 管理。

    文档：https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._expire_at: float = 0.0

    async def get_tenant_access_token(self) -> str:
        now = time.time()
        if self._token and now < self._expire_at - 60:
            return self._token

        url = f"{settings.feishu_api_base}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": settings.feishu_app_id,
            "app_secret": settings.feishu_app_secret,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.error(
                "获取 tenant_access_token 失败，怀疑 App ID/Secret 配置错误或应用未启用。响应: %s",
                data,
            )
            raise RuntimeError(f"获取 tenant_access_token 失败: {data.get('msg')}")

        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError("tenant_access_token 响应为空，无法调用飞书 API")

        expire = int(data.get("expire", 7200))
        self._token = token
        self._expire_at = now + expire
        return token


class FeishuApiClient:
    def __init__(
        self,
        auth: FeishuAuthClient | None = None,
        user_auth: FeishuUserAuthClient | None = None,
    ) -> None:
        self._auth = auth or FeishuAuthClient()
        self._user_auth = user_auth or FeishuUserAuthClient()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        expect_json: bool = True,
        use_user_token: bool = False,
    ) -> Any:
        if use_user_token:
            return await self._request_with_token(
                method,
                path,
                params=params,
                json_body=json_body,
                expect_json=expect_json,
                user_token=True,
            )
        return await self._request_with_token(
            method,
            path,
            params=params,
            json_body=json_body,
            expect_json=expect_json,
            user_token=False,
        )

    async def _request_with_token(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        expect_json: bool = True,
        user_token: bool,
    ) -> Any:
        for attempt in range(2):
            if user_token:
                token = await self._user_auth.get_user_access_token(force_refresh=attempt > 0)
            else:
                token = await self._auth.get_tenant_access_token()

            url = f"{settings.feishu_api_base}{path}"
            headers = {"Authorization": f"Bearer {token}"}
            if json_body is not None:
                headers["Content-Type"] = "application/json; charset=utf-8"

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                )

            if not expect_json:
                response.raise_for_status()
                return response

            data = response.json()
            code = data.get("code")
            if code == 0:
                return data.get("data", {})

            if user_token and code in TOKEN_INVALID_CODES and attempt == 0:
                logger.warning(
                    "user_access_token 失效 path=%s，尝试 refresh 后重试",
                    path,
                )
                continue

            logger.error(
                "飞书 API 调用失败 path=%s code=%s msg=%s，怀疑权限不足或资源未就绪",
                path,
                code,
                data.get("msg"),
            )
            raise RuntimeError(
                f"飞书 API 错误 [{code}]: {data.get('msg')} (path={path})"
            )

        raise RuntimeError(f"飞书 API 重试后仍失败 (path={path})")

    async def download_url(self, url: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response
