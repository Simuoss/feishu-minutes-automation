"""本地导入：把用户的音视频或文档收成一场会议。

大文件走 R2 直传：浏览器 PUT 到边缘节点，服务器再从 R2 拉回本地（R2 出流量免费）。
这条路绕开了 Cloudflare 隧道，几百兆的录屏不至于卡死。R2 没开或文件很小时，
退回 multipart 直传服务器。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.core.auth_context import require_user_id
from app.integrations.r2_client import R2ClientError, r2_client
from app.service.meeting_import_service import (
    MAX_UPLOAD_BYTES,
    MeetingImportError,
    classify,
    meeting_import_service,
    new_import_token,
    sanitize_filename,
)
from app.service.r2_media_service import r2_media_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/imports", tags=["imports"])


class PresignRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=1)


class PresignResponse(BaseModel):
    minute_token: str
    object_key: str
    upload_url: str
    # 服务器从 R2 拉回时会用到，前端原样回传
    expires_in: int


class CommitRequest(BaseModel):
    minute_token: str = Field(min_length=1, max_length=32)
    object_key: str = Field(min_length=1, max_length=512)
    filename: str = Field(min_length=1, max_length=255)
    title: str | None = Field(default=None, max_length=200)


class ImportResponse(BaseModel):
    minute_token: str
    title: str
    kind: str
    duration_ms: int | None = None
    has_video: bool = False
    message: str


def _import_object_key(minute_token: str, *, owner_user_id: int, filename: str) -> str:
    """临时对象放在会议自己的前缀下，这样归属校验能直接复用。"""
    return (
        f"meetings/{owner_user_id}/{minute_token}/import/"
        f"{sanitize_filename(filename)}"
    )


@router.post("/presign", response_model=PresignResponse)
async def presign_import(payload: PresignRequest, request: Request) -> PresignResponse:
    """签一个直传 R2 的 PUT URL。"""
    owner = require_user_id(request)
    if not r2_media_service.enabled():
        raise HTTPException(
            status_code=400, detail="未启用 R2 存储，请改用直接上传"
        )
    if payload.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"单个文件上限 {MAX_UPLOAD_BYTES // (1024**3)}GB，"
                "更大的文件请放到服务器本地目录再导入"
            ),
        )
    try:
        classify(payload.filename)
    except MeetingImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token = new_import_token()
    key = _import_object_key(token, owner_user_id=owner, filename=payload.filename)
    try:
        url = await r2_client.presign_put(key)
    except R2ClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return PresignResponse(
        minute_token=token, object_key=key, upload_url=url, expires_in=7200
    )


@router.post("/commit", response_model=ImportResponse)
async def commit_import(payload: CommitRequest, request: Request) -> ImportResponse:
    """直传完成后从 R2 把文件拉回本地，收成会议，并清掉临时对象。"""
    owner = require_user_id(request)
    expected = _import_object_key(
        payload.minute_token, owner_user_id=owner, filename=payload.filename
    )
    if payload.object_key != expected:
        # key 由服务器算，客户端只能原样回传；不一致就是在试别人的前缀
        raise HTTPException(status_code=400, detail="对象路径与本次导入不匹配")
    if not await r2_client.exists(payload.object_key):
        raise HTTPException(
            status_code=400, detail="R2 上找不到上传的文件，请重新上传"
        )

    staging = Path(tempfile.gettempdir()) / "lark-import" / payload.minute_token
    local = staging / sanitize_filename(payload.filename)
    try:
        await r2_client.download_file(payload.object_key, local)
        result = await meeting_import_service.import_file(
            local,
            owner_user_id=owner,
            filename=payload.filename,
            title=payload.title,
            minute_token=payload.minute_token,
        )
    except MeetingImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        # 临时对象删不掉只是留个孤儿，不该影响导入结果
        await r2_client.delete_object(payload.object_key)

    return _to_response(result)


@router.post("/upload", response_model=ImportResponse)
async def upload_import(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
) -> ImportResponse:
    """兜底路径：文件直接传给服务器。R2 关闭时或小文档用这条。"""
    owner = require_user_id(request)
    filename = file.filename or "import"
    try:
        classify(filename)
    except MeetingImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token = new_import_token()
    staging = Path(tempfile.gettempdir()) / "lark-import" / token
    staging.mkdir(parents=True, exist_ok=True)
    local = staging / sanitize_filename(filename)
    written = 0
    try:
        with local.open("wb") as handle:
            while chunk := await file.read(4 * 1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"文件超过 {MAX_UPLOAD_BYTES // (1024**3)}GB 上限",
                    )
                handle.write(chunk)
        result = await meeting_import_service.import_file(
            local,
            owner_user_id=owner,
            filename=filename,
            title=title,
            minute_token=token,
        )
    except MeetingImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return _to_response(result)


def _to_response(result) -> ImportResponse:
    if result.kind == "media":
        message = "已导入，正在排队转写与生成纪要"
    else:
        message = "已导入文本，正在排队生成纪要"
    return ImportResponse(
        minute_token=result.minute_token,
        title=result.title,
        kind=result.kind,
        duration_ms=result.duration_ms,
        has_video=result.has_video,
        message=message,
    )
