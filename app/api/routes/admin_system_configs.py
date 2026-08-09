"""超级管理端：系统业务配置读写。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth_context import require_super_admin
from app.data_model.entity.system_config import SystemConfigCreateEntity
from app.service.system_config_service import system_config_service

router = APIRouter(prefix="/admin/system-configs", tags=["admin-system-configs"])


class SystemConfigItemResponse(BaseModel):
    key: str
    description: str
    value: str
    remark: str
    updated_at: int
    updated_at_text: str


class SystemConfigListResponse(BaseModel):
    items: list[SystemConfigItemResponse]
    total: int


class SystemConfigUpdateRequest(BaseModel):
    description: str | None = None
    value: str | None = None
    remark: str | None = None


class SystemConfigCreateRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    value: str = ""
    remark: str = ""


def _ms_text(ms: int) -> str:
    if not ms:
        return "—"
    value = ms / 1000.0 if ms > 10_000_000_000 else float(ms)
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M"
    )


def _to_response(entity) -> SystemConfigItemResponse:
    return SystemConfigItemResponse(
        key=entity.key,
        description=entity.description,
        value=entity.value,
        remark=entity.remark,
        updated_at=entity.updated_at,
        updated_at_text=_ms_text(entity.updated_at),
    )


@router.get("", response_model=SystemConfigListResponse)
async def list_system_configs(request: Request) -> SystemConfigListResponse:
    require_super_admin(request)
    items = await system_config_service.list_all()
    return SystemConfigListResponse(
        items=[_to_response(row) for row in items],
        total=len(items),
    )


@router.put("/{key}", response_model=SystemConfigItemResponse)
async def update_system_config(
    key: str, body: SystemConfigUpdateRequest, request: Request
) -> SystemConfigItemResponse:
    require_super_admin(request)
    if body.description is None and body.value is None and body.remark is None:
        raise HTTPException(status_code=400, detail="至少提供一个可更新字段")
    updated = await system_config_service.update(
        key,
        description=body.description,
        value=body.value,
        remark=body.remark,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="配置项不存在")
    return _to_response(updated)


@router.post("", response_model=SystemConfigItemResponse)
async def create_system_config(
    body: SystemConfigCreateRequest, request: Request
) -> SystemConfigItemResponse:
    require_super_admin(request)
    try:
        created = await system_config_service.create(
            SystemConfigCreateEntity(
                key=body.key,
                description=body.description,
                value=body.value,
                remark=body.remark,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(created)
