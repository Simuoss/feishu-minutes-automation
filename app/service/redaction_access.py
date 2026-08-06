"""脱敏配图访问控制：禁止未打码敏感原图经 API 流出。"""

from __future__ import annotations

import logging

from app.service.metadata_db_service import read_redaction_audit

logger = logging.getLogger(__name__)

# 仅这些状态允许通过 summary/assets 下发（已是打码结果或本就不敏感）
_SAFE_ASSET_STATUSES = frozenset({"CLEAN", "REDACTED", "MANUAL_APPROVED"})


def figure_id_from_asset_filename(filename: str) -> str | None:
    name = (filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in {".", ".."} or "/" in name:
        return None
    stem = name.rsplit(".", 1)[0]
    if not stem.startswith("fig-"):
        return None
    return stem


async def assert_asset_safe_to_serve(
    minute_token: str, filename: str, *, owner_user_id: int
) -> None:
    """敏感且未完成打码、或已放弃的配图，不得通过 assets 接口展示。

    审计读失败或缺失时 fail-closed：有 figure_id 就拒绝，避免未打码图外泄。
    """
    from fastapi import HTTPException

    figure_id = figure_id_from_asset_filename(filename)
    if figure_id is None:
        return
    try:
        audit = await read_redaction_audit(
            minute_token, owner_user_id=owner_user_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "读取脱敏审计失败，拒绝下发配图以免未打码内容外泄。"
            "token=%s figure=%s owner=%s err=%s",
            minute_token,
            figure_id,
            owner_user_id,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail="脱敏审计暂不可用，暂时无法展示配图",
        ) from exc
    if not audit:
        # 历史会议可能从未跑过脱敏流水线；无审计时放行，但读库失败绝不能放行
        return
    for item in audit.get("figures") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("figure_id") or "") != figure_id:
            continue
        status = str(item.get("status") or "CLEAN").upper()
        sensitive = bool(item.get("sensitive"))
        if status == "ABANDONED":
            raise HTTPException(
                status_code=404,
                detail="该配图已放弃脱敏，不可展示",
            )
        if sensitive and status not in _SAFE_ASSET_STATUSES:
            raise HTTPException(
                status_code=403,
                detail="该配图尚未完成脱敏，禁止展示未打码内容",
            )
        return
    # 审计中未登记该 figure：不额外拦截（完整清单由生成流水线写入）
