import re
from dataclasses import dataclass
from typing import Any

TOKEN_INVALID_CODES = {99991663, 99991664, 99991665, 99991666, 99991668}
PERMISSION_DENIED_CODE = 99991672
USER_SCOPE_DENIED_CODE = 99991679
# 妙记已创建但转写/媒体尚未就绪，稍后重试即可
MINUTE_NOT_READY_CODE = 2091003


@dataclass
class FeishuApiErrorInfo:
    code: int | None
    message: str
    needs_app_scope: bool = False
    scope_auth_url: str | None = None
    needs_user_login: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.code,
            "error_message": self.message,
            "needs_app_scope": self.needs_app_scope,
            "scope_auth_url": self.scope_auth_url,
            "needs_user_login": self.needs_user_login,
        }


def parse_feishu_api_error(exc: BaseException) -> FeishuApiErrorInfo:
    message = str(exc)
    code: int | None = None
    match = re.search(r"\[(\d+)\]", message)
    if match:
        code = int(match.group(1))

    scope_url: str | None = None
    url_match = re.search(r"https://open\.feishu\.cn/app/[^\s)]+", message)
    if url_match:
        scope_url = url_match.group(0)

    needs_app_scope = code == PERMISSION_DENIED_CODE and "token_type=tenant" in message
    needs_user_login = code in TOKEN_INVALID_CODES or code == USER_SCOPE_DENIED_CODE or (
        code == PERMISSION_DENIED_CODE and "token_type=tenant" not in message
    )

    return FeishuApiErrorInfo(
        code=code,
        message=message,
        needs_app_scope=needs_app_scope,
        scope_auth_url=scope_url,
        needs_user_login=needs_user_login,
    )
