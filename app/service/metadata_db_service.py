"""元数据 DB 读写（会议/纪要/配图/脱敏/R2 权威来源）。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.data_model.entity.figure import FigureUpsertEntity
from app.data_model.entity.meeting_record import MeetingRecordCreateEntity
from app.data_model.entity.r2_sync_state import R2SyncStateUpsertEntity
from app.data_model.entity.redaction_figure import RedactionFigureUpsertEntity
from app.data_model.entity.summary_run import SummaryRunEntity, SummaryRunUpsertEntity
from app.repository.uow import UnitOfWork

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _normalize_create_time(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text[:64] if text else None


def _parse_downloaded_at_ms(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = int(raw)
        # 秒级时间戳抬到毫秒
        return value * 1000 if value < 10_000_000_000 else value
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def require_owner_user_id(explicit: int | None) -> int:
    """必须有调用方显式传入的归属用户；禁止 ContextVar / 默认账号回退。"""
    if explicit is None:
        raise RuntimeError("缺少 owner_user_id，拒绝写入无归属数据（禁止回退到其他账号）")
    return int(explicit)


async def upsert_meeting_meta(
    minute_token: str,
    meta: dict[str, Any],
    *,
    owner_user_id: int | None = None,
) -> None:
    token = (minute_token or "").strip()
    if not token:
        return
    from app.core.resource_key import storage_relpath

    owner_id = require_owner_user_id(owner_user_id)
    rel = storage_relpath(owner_id, token)
    files = meta.get("files") if isinstance(meta.get("files"), dict) else {}
    event_id = str(meta.get("feishu_event_id") or f"meta:{token}:u{owner_id}")
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        existing = await uow.meeting_records.get_by_event_id(event_id)
        if existing is None:
            existing = await uow.meeting_records.get_latest_by_minute_token(
                token, owner_user_id=owner_id
            )
        entity = MeetingRecordCreateEntity(
            feishu_event_id=event_id if existing is None else existing.feishu_event_id,
            event_type=existing.event_type if existing else "META_BACKFILL",
            unique_key=meta.get("unique_key") or (existing.unique_key if existing else None),
            minute_token=token,
            title=meta.get("title"),
            duration_ms=meta.get("duration_ms"),
            has_video=meta.get("has_video"),
            # meta.json 表示已落盘，默认 COMPLETED；若库里仍是 DOWNLOADING 则提升
            status=(
                "COMPLETED"
                if not existing or existing.status in {None, "PENDING", "DOWNLOADING", "FAILED"}
                else existing.status
            ),
            storage_path=existing.storage_path if existing else rel,
            owner_user_id=owner_id,
            summary_status=existing.summary_status if existing else None,
            media_relpath=files.get("media") if isinstance(files, dict) else None,
            transcript_relpath=files.get("transcript") if isinstance(files, dict) else None,
            downloaded_at=_parse_downloaded_at_ms(meta.get("downloaded_at")),
            storage_root_relpath=rel,
            create_time=_normalize_create_time(meta.get("create_time")),
        )
        if existing and existing.id is not None:
            from app.data_model.entity.meeting_record import MeetingRecordUpdateEntity

            await uow.meeting_records.update(
                MeetingRecordUpdateEntity(
                    id=existing.id,
                    minute_token=entity.minute_token,
                    title=entity.title,
                    duration_ms=entity.duration_ms,
                    has_video=entity.has_video,
                    unique_key=entity.unique_key,
                    owner_user_id=entity.owner_user_id,
                    media_relpath=entity.media_relpath,
                    transcript_relpath=entity.transcript_relpath,
                    downloaded_at=entity.downloaded_at,
                    storage_root_relpath=entity.storage_root_relpath,
                    status=entity.status,
                    storage_path=entity.storage_path,
                    create_time=entity.create_time,
                )
            )
        else:
            await uow.meeting_records.create(entity)
        await uow.commit()


def summary_run_to_meta(entity: SummaryRunEntity) -> dict[str, Any]:
    return {
        "minute_token": entity.minute_token,
        "model": entity.model,
        "input_tokens": entity.input_tokens,
        "output_tokens": entity.output_tokens,
        "attempts": entity.attempts,
        "elapsed_seconds": entity.elapsed_seconds,
        "stop_reason": entity.stop_reason,
        "truncated": entity.truncated,
        "transcript_chars": entity.transcript_chars,
        "summary_chars": entity.summary_chars,
        "anchor_total": entity.anchor_total,
        "anchor_aligned": entity.anchor_aligned,
        "anchor_suspicious": entity.anchor_suspicious,
        "figure_planned": entity.figure_planned,
        "figure_for_writing": entity.figure_for_writing,
        "figure_used": entity.figure_used,
        "figure_scanned": entity.figure_scanned,
        "figure_sensitive": entity.figure_sensitive,
        "figure_redacted": entity.figure_redacted,
        "figure_abandoned": entity.figure_abandoned,
        "run_mode": entity.run_mode,
        "scene": entity.scene,
        "scene_reason": entity.scene_reason,
        "generated_at": entity.generated_at,
        "redacted_at": entity.redacted_at,
        "r2_synced_at": entity.r2_synced_at,
        "r2_uploaded": entity.r2_uploaded,
    }


def meta_to_summary_upsert(
    minute_token: str, meta: dict[str, Any], *, owner_user_id: int
) -> SummaryRunUpsertEntity:
    owner_id = require_owner_user_id(owner_user_id)
    suspicious = meta.get("anchor_suspicious")
    if isinstance(suspicious, int):
        suspicious_list: list[Any] = []
    elif isinstance(suspicious, list):
        suspicious_list = suspicious
    else:
        suspicious_list = []
    return SummaryRunUpsertEntity(
        owner_user_id=owner_id,
        minute_token=minute_token,
        model=meta.get("model"),
        input_tokens=meta.get("input_tokens") if isinstance(meta.get("input_tokens"), int) else None,
        output_tokens=meta.get("output_tokens") if isinstance(meta.get("output_tokens"), int) else None,
        attempts=meta.get("attempts") if isinstance(meta.get("attempts"), int) else None,
        elapsed_seconds=meta.get("elapsed_seconds")
        if isinstance(meta.get("elapsed_seconds"), (int, float))
        else None,
        stop_reason=meta.get("stop_reason"),
        truncated=meta.get("truncated") if isinstance(meta.get("truncated"), bool) else None,
        transcript_chars=meta.get("transcript_chars")
        if isinstance(meta.get("transcript_chars"), int)
        else None,
        summary_chars=meta.get("summary_chars")
        if isinstance(meta.get("summary_chars"), int)
        else None,
        anchor_total=meta.get("anchor_total") if isinstance(meta.get("anchor_total"), int) else None,
        anchor_aligned=meta.get("anchor_aligned")
        if isinstance(meta.get("anchor_aligned"), int)
        else None,
        figure_planned=meta.get("figure_planned")
        if isinstance(meta.get("figure_planned"), int)
        else None,
        figure_for_writing=meta.get("figure_for_writing")
        if isinstance(meta.get("figure_for_writing"), int)
        else None,
        figure_used=meta.get("figure_used") if isinstance(meta.get("figure_used"), int) else None,
        figure_scanned=meta.get("figure_scanned")
        if isinstance(meta.get("figure_scanned"), int)
        else None,
        figure_sensitive=meta.get("figure_sensitive")
        if isinstance(meta.get("figure_sensitive"), int)
        else None,
        figure_redacted=meta.get("figure_redacted")
        if isinstance(meta.get("figure_redacted"), int)
        else None,
        figure_abandoned=meta.get("figure_abandoned")
        if isinstance(meta.get("figure_abandoned"), int)
        else None,
        run_mode=meta.get("run_mode"),
        scene=meta.get("scene"),
        scene_reason=meta.get("scene_reason"),
        generated_at=meta.get("generated_at"),
        redacted_at=meta.get("redacted_at"),
        r2_synced_at=meta.get("r2_synced_at"),
        r2_uploaded=meta.get("r2_uploaded") if isinstance(meta.get("r2_uploaded"), int) else None,
        summary_relpath="output/summary.md",
        anchor_suspicious=suspicious_list,
        updated_at=_now_ms(),
    )


async def upsert_summary_meta(
    minute_token: str,
    meta: dict[str, Any],
    *,
    owner_user_id: int,
) -> None:
    token = (minute_token or "").strip()
    if not token or not meta:
        return
    owner_id = require_owner_user_id(owner_user_id)
    async with UnitOfWork() as uow:
        assert uow.summary_runs is not None
        assert uow.meeting_records is not None
        await uow.summary_runs.upsert(
            meta_to_summary_upsert(token, meta, owner_user_id=owner_id)
        )
        record = await uow.meeting_records.get_latest_by_minute_token(
            token, owner_user_id=owner_id
        )
        if record and record.id is not None:
            from app.data_model.entity.meeting_record import MeetingRecordUpdateEntity

            await uow.meeting_records.update(
                MeetingRecordUpdateEntity(id=record.id, summary_status="COMPLETED")
            )
        await uow.commit()


async def replace_figures_from_manifest(
    minute_token: str, manifest: dict[str, Any], *, owner_user_id: int
) -> None:
    token = (minute_token or "").strip()
    if not token:
        return
    owner_id = require_owner_user_id(owner_user_id)
    figures_raw = manifest.get("figures") if isinstance(manifest, dict) else None
    items: list[FigureUpsertEntity] = []
    if isinstance(figures_raw, list):
        for item in figures_raw:
            if not isinstance(item, dict):
                continue
            figure_id = str(item.get("figure_id") or "").strip()
            if not figure_id:
                continue
            frame_seconds = item.get("frame_seconds")
            items.append(
                FigureUpsertEntity(
                    owner_user_id=owner_id,
                    minute_token=token,
                    figure_id=figure_id,
                    relative_path=item.get("relative_path"),
                    timestamp=item.get("timestamp"),
                    frame_seconds=float(frame_seconds)
                    if isinstance(frame_seconds, (int, float))
                    else None,
                    expect=item.get("expect"),
                    purpose=item.get("purpose"),
                    status="READY",
                )
            )
    async with UnitOfWork() as uow:
        assert uow.figures is not None
        await uow.figures.replace_for_minute(token, items, owner_user_id=owner_id)
        await uow.commit()


async def replace_redaction_from_audit(
    minute_token: str, audit: dict[str, Any], *, owner_user_id: int
) -> None:
    token = (minute_token or "").strip()
    if not token:
        return
    owner_id = require_owner_user_id(owner_user_id)
    figures_raw = audit.get("figures") if isinstance(audit, dict) else None
    items: list[RedactionFigureUpsertEntity] = []
    if isinstance(figures_raw, list):
        for item in figures_raw:
            if not isinstance(item, dict):
                continue
            figure_id = str(item.get("figure_id") or "").strip()
            if not figure_id:
                continue
            regions = item.get("regions") if isinstance(item.get("regions"), list) else []
            attempts = item.get("attempts") if isinstance(item.get("attempts"), list) else []
            items.append(
                RedactionFigureUpsertEntity(
                    owner_user_id=owner_id,
                    minute_token=token,
                    figure_id=figure_id,
                    sensitive=bool(item.get("sensitive")),
                    status=str(item.get("status") or "CLEAN").upper(),
                    abandon_reason=item.get("abandon_reason"),
                    original_relpath=item.get("original_relative"),
                    asset_relpath=item.get("asset_relative"),
                    regions=regions,
                    attempts=attempts,
                )
            )
    async with UnitOfWork() as uow:
        assert uow.redaction_figures is not None
        await uow.redaction_figures.replace_for_minute(
            token, items, owner_user_id=owner_id
        )
        await uow.commit()


async def upsert_r2_meta(
    minute_token: str, meta: dict[str, Any], *, owner_user_id: int
) -> None:
    token = (minute_token or "").strip()
    if not token:
        return
    owner_id = require_owner_user_id(owner_user_id)
    video = meta.get("video") if isinstance(meta.get("video"), dict) else {}
    assets = meta.get("assets") if isinstance(meta.get("assets"), dict) else {}
    text = meta.get("text") if isinstance(meta.get("text"), dict) else {}
    async with UnitOfWork() as uow:
        assert uow.r2_sync_states is not None
        await uow.r2_sync_states.upsert(
            R2SyncStateUpsertEntity(
                owner_user_id=owner_id,
                minute_token=token,
                video_key=video.get("key"),
                video_etag=video.get("etag"),
                video_status=video.get("status"),
                video_updated_at=video.get("updated_at"),
                assets=assets,
                text=text,
                updated_at=_now_ms(),
            )
        )
        await uow.commit()


def _ms_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    seconds = value / 1000.0 if value > 10_000_000_000 else float(value)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


async def read_meeting_meta(
    minute_token: str, *, owner_user_id: int
) -> dict[str, Any] | None:
    """读取指定 owner 下的会议元数据；必须显式传 owner_user_id。"""
    token = (minute_token or "").strip()
    if not token:
        return None
    owner_id = require_owner_user_id(owner_user_id)
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        record = await uow.meeting_records.get_latest_by_minute_token(
            token, owner_user_id=owner_id
        )
        if record is None:
            return None
        return {
            "minute_token": record.minute_token,
            "title": record.title,
            "duration_ms": record.duration_ms,
            "has_video": record.has_video,
            "feishu_event_id": record.feishu_event_id,
            "unique_key": record.unique_key,
            "downloaded_at": _ms_to_iso(record.downloaded_at),
            "create_time": record.create_time,
            "files": {
                "media": record.media_relpath,
                "transcript": record.transcript_relpath,
            },
            "status": record.status,
            "summary_status": record.summary_status,
        }


async def read_figures_manifest(
    minute_token: str, *, owner_user_id: int
) -> dict[str, Any] | None:
    token = (minute_token or "").strip()
    if not token:
        return None
    owner_id = require_owner_user_id(owner_user_id)
    async with UnitOfWork() as uow:
        assert uow.figures is not None
        items = await uow.figures.list_by_minute_token(token, owner_user_id=owner_id)
        if not items:
            return None
        return {
            "figures": [
                {
                    "figure_id": item.figure_id,
                    "relative_path": item.relative_path,
                    "timestamp": item.timestamp,
                    "frame_seconds": item.frame_seconds,
                    "expect": item.expect,
                    "purpose": item.purpose,
                    "status": item.status,
                }
                for item in items
            ]
        }


async def read_summary_meta(
    minute_token: str, *, owner_user_id: int
) -> dict[str, Any] | None:
    owner_id = require_owner_user_id(owner_user_id)
    async with UnitOfWork() as uow:
        assert uow.summary_runs is not None
        entity = await uow.summary_runs.get_by_minute_token(
            minute_token, owner_user_id=owner_id
        )
        if entity is None:
            return None
        return summary_run_to_meta(entity)


async def read_redaction_audit(
    minute_token: str, *, owner_user_id: int
) -> dict[str, Any] | None:
    owner_id = require_owner_user_id(owner_user_id)
    async with UnitOfWork() as uow:
        assert uow.redaction_figures is not None
        items = await uow.redaction_figures.list_by_minute_token(
            minute_token, owner_user_id=owner_id
        )
        if not items:
            return None
        figures = []
        for item in items:
            figures.append(
                {
                    "figure_id": item.figure_id,
                    "sensitive": item.sensitive,
                    "status": item.status,
                    "regions": item.regions,
                    "attempts": item.attempts,
                    "abandon_reason": item.abandon_reason,
                    "original_relative": item.original_relpath,
                    "asset_relative": item.asset_relpath,
                }
            )
        sensitive = sum(1 for f in figures if f.get("sensitive"))
        redacted = sum(
            1 for f in figures if f.get("status") in {"REDACTED", "MANUAL_APPROVED"}
        )
        abandoned = sum(1 for f in figures if f.get("status") == "ABANDONED")
        return {
            "scanned": len(figures),
            "sensitive": sensitive,
            "redacted": redacted,
            "abandoned": abandoned,
            "figures": figures,
        }


async def read_r2_meta(
    minute_token: str, *, owner_user_id: int
) -> dict[str, Any] | None:
    owner_id = require_owner_user_id(owner_user_id)
    async with UnitOfWork() as uow:
        assert uow.r2_sync_states is not None
        entity = await uow.r2_sync_states.get_by_minute_token(
            minute_token, owner_user_id=owner_id
        )
        if entity is None:
            return None
        video: dict[str, Any] = {}
        if entity.video_key or entity.video_status:
            video = {
                "key": entity.video_key,
                "etag": entity.video_etag,
                "status": entity.video_status,
                "updated_at": entity.video_updated_at,
            }
        return {
            "assets": entity.assets or {},
            "video": video,
            "text": entity.text or {},
        }



async def set_summary_status(
    minute_token: str,
    status: str,
    *,
    owner_user_id: int,
) -> None:
    """按 owner+token 更新会议 summary_status。"""
    token = (minute_token or "").strip()
    if not token:
        return
    from app.data_model.entity.meeting_record import (
        MeetingRecordQueryEntity,
        MeetingRecordUpdateEntity,
    )

    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        records = await uow.meeting_records.query(
            MeetingRecordQueryEntity(minute_token=token, owner_user_id=owner_user_id)
        )
        if not records:
            return
        for record in records:
            if record.id is None:
                continue
            await uow.meeting_records.update(
                MeetingRecordUpdateEntity(id=record.id, summary_status=status)
            )
        await uow.commit()
