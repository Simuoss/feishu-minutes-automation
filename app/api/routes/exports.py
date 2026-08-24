import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app.dto.export import ExportBatchRequest
from app.service.export_service import export_service
from app.service.http_download import content_disposition
from app.service.ownership import assert_meeting_readable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["exports"])


@router.post("/export/batch")
async def export_batch(body: ExportBatchRequest, request: Request) -> Response:
    """把选中会议的纪要/转写打成 zip。单场缺文件会写进包里的跳过清单。"""
    if not body.include_summary and not body.include_transcript:
        raise HTTPException(status_code=400, detail="至少勾选纪要或转写其中一项")

    resolved: list[tuple[str, int]] = []
    for item in body.items:
        token = (item.minute_token or "").strip()
        if not token:
            continue
        try:
            owner = await assert_meeting_readable(
                request, token, owner_user_id=item.owner_user_id
            )
        except HTTPException:
            # 没权限或没落库的当成没有，不让整包失败
            continue
        resolved.append((token, owner))
    if not resolved:
        raise HTTPException(status_code=404, detail="选中的会议里没有可导出的纪要或转写")

    try:
        data, filename = await asyncio.to_thread(
            export_service.export_batch,
            resolved,
            include_summary=body.include_summary,
            include_transcript=body.include_transcript,
            summary_format=body.summary_format,
            transcript_format=body.transcript_format,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("批量导出失败：%s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"导出失败：{exc}") from exc

    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.get("/{minute_token}/export/summary")
async def export_summary(
    minute_token: str,
    request: Request,
    format: str = Query(..., pattern="^(pdf|docx|md)$"),
    owner_user_id: int | None = None,
) -> Response:
    owner = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )
    try:
        data, filename, media_type = export_service.export_summary(
            minute_token, format, owner_user_id=owner
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "纪要导出失败 token=%s format=%s，怀疑依赖缺失或内容无法渲染: %s",
            minute_token,
            format,
            exc,
        )
        raise HTTPException(status_code=500, detail=f"导出失败：{exc}") from exc
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.get("/{minute_token}/export/transcript")
async def export_transcript(
    minute_token: str,
    request: Request,
    format: str = Query(..., pattern="^(md|txt)$"),
    owner_user_id: int | None = None,
) -> Response:
    owner = await assert_meeting_readable(
        request, minute_token, owner_user_id=owner_user_id
    )
    try:
        data, filename, media_type = export_service.export_transcript(
            minute_token, format, owner_user_id=owner
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": content_disposition(filename)},
    )
