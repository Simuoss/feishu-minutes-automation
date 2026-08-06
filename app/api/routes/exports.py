import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app.service.export_service import export_service
from app.service.http_download import content_disposition
from app.service.ownership import assert_meeting_readable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["exports"])


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
