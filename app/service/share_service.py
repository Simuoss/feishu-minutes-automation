from __future__ import annotations

import logging
import secrets

from app.core.config import settings
from app.data_model.entity.share import (
    ShareAdminItemEntity,
    ShareAdminListEntity,
    ShareAdminQueryEntity,
    ShareBatchEnsureEntity,
    ShareBatchItemEntity,
    ShareBatchResultEntity,
    ShareBatchResultItemEntity,
    ShareBatchRevokeEntity,
    ShareBatchUpdateEntity,
    ShareCreateEntity,
    ShareEntity,
    ShareLibraryItemEntity,
    ShareLibraryKeyStatusEntity,
    ShareLibraryResultEntity,
    ShareUpdateEntity,
)
from app.data_model.entity.share_access_log import ShareAccessLogCreateEntity
from app.repository.uow import UnitOfWork
from app.service.access_key_service import access_key_service, hash_access_key
from app.service.meeting_storage_service import MeetingStorageService
from app.service.share_session_store import ShareSession, share_session_store
from app.service.time_utils import utc_now_ms

logger = logging.getLogger(__name__)

ACCESS_MODE_PUBLIC = "PUBLIC"
ACCESS_MODE_KEY_REQUIRED = "KEY_REQUIRED"
ACTION_VIEW = "VIEW"
ACTION_EXPORT = "EXPORT"
ACTION_MEDIA = "MEDIA"


class ShareService:
    def __init__(self) -> None:
        self._storage = MeetingStorageService()

    def share_url(self, share_token: str) -> str:
        origin = settings.frontend_origin.rstrip("/")
        return f"{origin}/share.html?s={share_token}"

    async def _normalize_share_options(
        self,
        *,
        access_mode: str,
        allow_export: bool,
        access_key_id: int | None,
    ) -> tuple[str, bool, int | None]:
        access_mode = access_mode.upper()
        if access_mode not in {ACCESS_MODE_PUBLIC, ACCESS_MODE_KEY_REQUIRED}:
            raise ValueError("access_mode 只能是 PUBLIC 或 KEY_REQUIRED")
        if access_mode == ACCESS_MODE_KEY_REQUIRED:
            if access_key_id is None:
                raise ValueError("KEY_REQUIRED 分享必须选择一把密钥")
            key = await access_key_service.get(access_key_id)
            if key is None or not access_key_service.is_usable(key):
                raise ValueError("所选密钥不存在、已吊销或已过期")
            return access_mode, bool(allow_export), access_key_id
        return access_mode, bool(allow_export), None

    @staticmethod
    def _share_matches(
        share: ShareEntity,
        *,
        access_mode: str,
        allow_export: bool,
        access_key_id: int | None,
    ) -> bool:
        if share.access_mode != access_mode:
            return False
        if bool(share.allow_export) != bool(allow_export):
            return False
        if access_mode == ACCESS_MODE_KEY_REQUIRED and share.access_key_id != access_key_id:
            return False
        return True

    async def create_share(
        self,
        *,
        minute_token: str,
        access_mode: str,
        allow_export: bool,
        access_key_id: int | None,
    ) -> ShareEntity:
        access_mode, allow_export, access_key_id = await self._normalize_share_options(
            access_mode=access_mode,
            allow_export=allow_export,
            access_key_id=access_key_id,
        )
        if self._storage.read_meta(minute_token) is None:
            raise LookupError("本地未找到该会议，请先下载")

        entity = ShareCreateEntity(
            share_token=secrets.token_urlsafe(16),
            minute_token=minute_token,
            access_mode=access_mode,
            allow_export=allow_export,
            access_key_id=access_key_id,
            created_at=utc_now_ms(),
        )
        async with UnitOfWork() as uow:
            assert uow.shares is not None
            created = await uow.shares.create(entity)
            await uow.commit()
        return created

    async def ensure_shares_batch(
        self, body: ShareBatchEnsureEntity
    ) -> list[ShareBatchItemEntity]:
        """批量创建/复用分享：一次校验密钥、一次查库、一次落库。"""
        access_mode, allow_export, access_key_id = await self._normalize_share_options(
            access_mode=body.access_mode,
            allow_export=body.allow_export,
            access_key_id=body.access_key_id,
        )

        seen: set[str] = set()
        ordered_tokens: list[str] = []
        for raw in body.minute_tokens:
            token = (raw or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            ordered_tokens.append(token)
        if not ordered_tokens:
            raise ValueError("minute_tokens 不能为空")

        async with UnitOfWork() as uow:
            assert uow.shares is not None
            existing = await uow.shares.list_active_by_minutes(ordered_tokens)
            by_minute: dict[str, list[ShareEntity]] = {}
            for share in existing:
                by_minute.setdefault(share.minute_token, []).append(share)

            results: list[ShareBatchItemEntity] = []
            created_now: dict[str, ShareEntity] = {}
            now = utc_now_ms()
            for minute_token in ordered_tokens:
                if self._storage.read_meta(minute_token) is None:
                    results.append(
                        ShareBatchItemEntity(
                            minute_token=minute_token,
                            error="本地未找到该会议，请先下载",
                        )
                    )
                    continue

                matched = next(
                    (
                        s
                        for s in by_minute.get(minute_token, [])
                        if self._share_matches(
                            s,
                            access_mode=access_mode,
                            allow_export=allow_export,
                            access_key_id=access_key_id,
                        )
                    ),
                    None,
                )
                if matched is not None:
                    results.append(
                        ShareBatchItemEntity(
                            minute_token=minute_token,
                            share=matched,
                            reused=True,
                        )
                    )
                    continue

                created = await uow.shares.create(
                    ShareCreateEntity(
                        share_token=secrets.token_urlsafe(16),
                        minute_token=minute_token,
                        access_mode=access_mode,
                        allow_export=allow_export,
                        access_key_id=access_key_id,
                        created_at=now,
                    )
                )
                created_now[minute_token] = created
                results.append(
                    ShareBatchItemEntity(
                        minute_token=minute_token,
                        share=created,
                        reused=False,
                    )
                )

            if created_now:
                await uow.commit()
                logger.info(
                    "批量分享完成：请求 %s 个，新建 %s 个，复用 %s 个",
                    len(ordered_tokens),
                    len(created_now),
                    sum(1 for r in results if r.reused),
                )
            return results

    async def list_shares(self, minute_token: str) -> list[ShareEntity]:
        async with UnitOfWork() as uow:
            assert uow.shares is not None
            return await uow.shares.list_by_minute(minute_token)

    async def list_all_shares(
        self,
        query: ShareAdminQueryEntity | None = None,
        *,
        include_revoked: bool = False,
        minute_token: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> ShareAdminListEntity:
        q = query or ShareAdminQueryEntity(
            status="ALL" if include_revoked else "ACTIVE",
            minute_token=minute_token,
            limit=limit,
            offset=offset,
        )
        async with UnitOfWork() as uow:
            assert uow.shares is not None
            rows = await uow.shares.list_all(q)

        items: list[ShareAdminItemEntity] = []
        keyword = (q.q or "").strip().lower()
        for share in rows:
            meta = self._storage.read_meta(share.minute_token) or {}
            title = meta.get("title")
            title_str = str(title) if title else None
            if keyword:
                hay = f"{title_str or ''} {share.minute_token} {share.share_token}".lower()
                if keyword not in hay:
                    continue
            items.append(ShareAdminItemEntity(share=share, title=title_str))

        sort_by = (q.sort_by or "CREATED_AT").upper()
        reverse = (q.sort_order or "DESC").upper() != "ASC"

        def sort_key(item: ShareAdminItemEntity):
            s = item.share
            if sort_by == "TITLE":
                return ((item.title or s.minute_token).lower(), s.id or 0)
            if sort_by == "ACCESS_KEY_ID":
                return (s.access_key_id is None, s.access_key_id or 0, s.id or 0)
            if sort_by == "ALLOW_EXPORT":
                return (bool(s.allow_export), s.id or 0)
            if sort_by == "ACCESS_MODE":
                return (s.access_mode, s.id or 0)
            return (s.created_at, s.id or 0)

        items.sort(key=sort_key, reverse=reverse)
        total = len(items)
        start = max(0, q.offset)
        end = start + max(1, min(q.limit, 500))
        return ShareAdminListEntity(items=items[start:end], total=total)

    async def update_share(self, body: ShareUpdateEntity) -> ShareEntity:
        async with UnitOfWork() as uow:
            assert uow.shares is not None
            row = await uow.shares.get_by_id(body.id)
            if row is None:
                raise LookupError("分享不存在")
            if row.revoked_at is not None:
                raise ValueError("已吊销的分享不能修改")

            new_mode = (
                body.access_mode.upper()
                if body.access_mode is not None
                else row.access_mode
            )
            new_export = (
                row.allow_export if body.allow_export is None else bool(body.allow_export)
            )
            if new_mode == ACCESS_MODE_PUBLIC:
                access_mode, allow_export, access_key_id = await self._normalize_share_options(
                    access_mode=ACCESS_MODE_PUBLIC,
                    allow_export=new_export,
                    access_key_id=None,
                )
                clear_access_key = True
            else:
                key_id = (
                    body.access_key_id
                    if body.access_key_id is not None
                    else row.access_key_id
                )
                access_mode, allow_export, access_key_id = await self._normalize_share_options(
                    access_mode=ACCESS_MODE_KEY_REQUIRED,
                    allow_export=new_export,
                    access_key_id=key_id,
                )
                clear_access_key = False

            updated = await uow.shares.update_fields(
                body.id,
                access_mode=access_mode,
                allow_export=allow_export,
                access_key_id=access_key_id,
                clear_access_key=clear_access_key,
            )
            await uow.commit()
            if updated is None:
                raise LookupError("分享不存在")

        cleared = share_session_store.invalidate_share(body.id)
        if cleared:
            logger.info(
                "分享权限已变更，已清掉 %s 个访客会话 share_id=%s",
                cleared,
                body.id,
            )
        return updated

    async def revoke_share(self, share_id: int) -> ShareEntity:
        async with UnitOfWork() as uow:
            assert uow.shares is not None
            row = await uow.shares.get_by_id(share_id)
            if row is None:
                raise LookupError("分享不存在")
            if row.revoked_at is not None:
                return row
            updated = await uow.shares.revoke(share_id, utc_now_ms())
            await uow.commit()
            if updated is None:
                raise LookupError("分享不存在")
        share_session_store.invalidate_share(share_id)
        return updated

    async def batch_update_shares(self, body: ShareBatchUpdateEntity) -> ShareBatchResultEntity:
        if not body.share_ids:
            raise ValueError("share_ids 不能为空")
        if (
            body.access_mode is None
            and body.allow_export is None
            and body.access_key_id is None
        ):
            raise ValueError("至少提供一个要修改的字段")

        results: list[ShareBatchResultItemEntity] = []
        success = 0
        for share_id in body.share_ids:
            try:
                updated = await self.update_share(
                    ShareUpdateEntity(
                        id=share_id,
                        access_mode=body.access_mode,
                        allow_export=body.allow_export,
                        access_key_id=body.access_key_id,
                    )
                )
                results.append(
                    ShareBatchResultItemEntity(id=share_id, ok=True, share=updated)
                )
                success += 1
            except (LookupError, ValueError) as exc:
                results.append(
                    ShareBatchResultItemEntity(id=share_id, ok=False, error=str(exc))
                )
            except Exception as exc:
                logger.error(
                    "批量更新分享失败，该条将被跳过。share_id=%s err=%s",
                    share_id,
                    exc,
                )
                results.append(
                    ShareBatchResultItemEntity(
                        id=share_id, ok=False, error="更新失败，请稍后重试"
                    )
                )
        return ShareBatchResultEntity(
            items=results,
            success_count=success,
            failed_count=len(results) - success,
        )

    async def batch_revoke_shares(self, body: ShareBatchRevokeEntity) -> ShareBatchResultEntity:
        if not body.share_ids:
            raise ValueError("share_ids 不能为空")
        results: list[ShareBatchResultItemEntity] = []
        success = 0
        for share_id in body.share_ids:
            try:
                updated = await self.revoke_share(share_id)
                results.append(
                    ShareBatchResultItemEntity(id=share_id, ok=True, share=updated)
                )
                success += 1
            except LookupError as exc:
                results.append(
                    ShareBatchResultItemEntity(id=share_id, ok=False, error=str(exc))
                )
            except Exception as exc:
                logger.error(
                    "批量吊销分享失败，该条将被跳过。share_id=%s err=%s",
                    share_id,
                    exc,
                )
                results.append(
                    ShareBatchResultItemEntity(
                        id=share_id, ok=False, error="吊销失败，请稍后重试"
                    )
                )
        return ShareBatchResultEntity(
            items=results,
            success_count=success,
            failed_count=len(results) - success,
        )

    async def get_by_token(self, share_token: str) -> ShareEntity | None:
        async with UnitOfWork() as uow:
            assert uow.shares is not None
            return await uow.shares.get_by_token(share_token)

    async def get_meta(self, share_token: str) -> dict:
        share = await self._require_active_share(share_token)
        meta = self._storage.read_meta(share.minute_token) or {}
        return {
            "share_token": share.share_token,
            "minute_token": share.minute_token,
            "title": meta.get("title") or share.minute_token,
            "access_mode": share.access_mode,
            "requires_key": share.access_mode == ACCESS_MODE_KEY_REQUIRED,
            "allow_export": share.allow_export,
        }

    async def unlock(
        self,
        share_token: str,
        plaintext_key: str,
        *,
        ip: str | None,
        user_agent: str | None,
    ) -> ShareSession:
        session, _prefix = await self.unlock_with_meta(
            share_token, plaintext_key, ip=ip, user_agent=user_agent
        )
        return session

    async def unlock_with_meta(
        self,
        share_token: str,
        plaintext_key: str,
        *,
        ip: str | None,
        user_agent: str | None,
    ) -> tuple[ShareSession, str]:
        share = await self._require_active_share(share_token)
        if share.access_mode != ACCESS_MODE_KEY_REQUIRED:
            raise ValueError("该分享为公开访问，无需密钥")
        if share.access_key_id is None:
            raise RuntimeError("分享未绑定密钥，数据异常")

        key_hash = hash_access_key(plaintext_key.strip())
        async with UnitOfWork() as uow:
            assert uow.access_keys is not None
            assert uow.share_access_logs is not None
            key = await uow.access_keys.get_by_hash(key_hash)
            if key is None or key.id != share.access_key_id:
                raise PermissionError("密钥无效")
            if not access_key_service.is_usable(key):
                raise PermissionError("密钥已过期或已吊销")
            await uow.share_access_logs.create(
                ShareAccessLogCreateEntity(
                    access_key_id=key.id,  # type: ignore[arg-type]
                    share_id=share.id,  # type: ignore[arg-type]
                    minute_token=share.minute_token,
                    action=ACTION_VIEW,
                    ip=ip,
                    user_agent=user_agent,
                    created_at=utc_now_ms(),
                )
            )
            await uow.commit()
            key_prefix = key.key_prefix

        session = share_session_store.create(
            share_id=share.id,  # type: ignore[arg-type]
            share_token=share.share_token,
            minute_token=share.minute_token,
            access_key_id=share.access_key_id,
            allow_export=share.allow_export,
        )
        return session, key_prefix

    async def try_unlock_with_keys(
        self,
        share_token: str,
        keys: list[str],
        *,
        ip: str | None,
        user_agent: str | None,
    ) -> tuple[ShareSession, str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in keys:
            value = (raw or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            cleaned.append(value)
        if not cleaned:
            raise ValueError("请至少提供一把密钥")

        last_perm: PermissionError | None = None
        for key in cleaned:
            try:
                return await self.unlock_with_meta(
                    share_token, key, ip=ip, user_agent=user_agent
                )
            except PermissionError as exc:
                last_perm = exc
                continue
        raise last_perm or PermissionError("密钥无效")

    async def resolve_library(
        self,
        *,
        keys: list[str],
        share_tokens: list[str] | None = None,
    ) -> ShareLibraryResultEntity:
        cleaned_keys: list[str] = []
        seen_keys: set[str] = set()
        for raw in keys or []:
            value = (raw or "").strip()
            if not value or value in seen_keys:
                continue
            seen_keys.add(value)
            cleaned_keys.append(value)
            if len(cleaned_keys) >= 30:
                break

        cleaned_tokens: list[str] = []
        seen_tokens: set[str] = set()
        for raw in share_tokens or []:
            value = (raw or "").strip()
            if not value or value in seen_tokens:
                continue
            seen_tokens.add(value)
            cleaned_tokens.append(value)
            if len(cleaned_tokens) >= 100:
                break

        key_id_to_prefix: dict[int, str] = {}
        usable_key_ids: list[int] = []
        hashes = [hash_access_key(k) for k in cleaned_keys]

        async with UnitOfWork() as uow:
            assert uow.access_keys is not None
            assert uow.shares is not None
            found_by_hash = {
                k.key_hash: k for k in await uow.access_keys.list_by_hashes(hashes)
            }

            pending_statuses: list[ShareLibraryKeyStatusEntity] = []
            for plaintext in cleaned_keys:
                digest = hash_access_key(plaintext)
                key = found_by_hash.get(digest)
                fallback_prefix = plaintext[:10] if len(plaintext) >= 10 else plaintext
                if key is None:
                    pending_statuses.append(
                        ShareLibraryKeyStatusEntity(
                            key_prefix=fallback_prefix, status="INVALID"
                        )
                    )
                    continue
                if key.revoked_at is not None:
                    pending_statuses.append(
                        ShareLibraryKeyStatusEntity(
                            key_prefix=key.key_prefix, status="REVOKED"
                        )
                    )
                    continue
                if not access_key_service.is_usable(key) or key.id is None:
                    pending_statuses.append(
                        ShareLibraryKeyStatusEntity(
                            key_prefix=key.key_prefix, status="EXPIRED"
                        )
                    )
                    continue
                usable_key_ids.append(key.id)
                key_id_to_prefix[key.id] = key.key_prefix
                pending_statuses.append(
                    ShareLibraryKeyStatusEntity(
                        key_prefix=key.key_prefix, status="VALID"
                    )
                )

            shares_by_key = await uow.shares.list_active_by_access_key_ids(usable_key_ids)
            known_shares: list[ShareEntity] = []
            for token in cleaned_tokens:
                row = await uow.shares.get_by_token(token)
                if row is not None and row.revoked_at is None:
                    known_shares.append(row)

        counts: dict[int, int] = {}
        for share in shares_by_key:
            if share.access_key_id is None:
                continue
            counts[share.access_key_id] = counts.get(share.access_key_id, 0) + 1

        key_statuses: list[ShareLibraryKeyStatusEntity] = []
        for status in pending_statuses:
            share_count = 0
            if status.status == "VALID":
                for kid, prefix in key_id_to_prefix.items():
                    if prefix == status.key_prefix:
                        share_count = counts.get(kid, 0)
                        break
            key_statuses.append(
                ShareLibraryKeyStatusEntity(
                    key_prefix=status.key_prefix,
                    status=status.status,
                    share_count=share_count,
                )
            )

        items_by_token: dict[str, ShareLibraryItemEntity] = {}
        for share in shares_by_key:
            meta = self._storage.read_meta(share.minute_token) or {}
            title = str(meta.get("title") or share.minute_token)
            items_by_token[share.share_token] = ShareLibraryItemEntity(
                share_token=share.share_token,
                minute_token=share.minute_token,
                title=title,
                access_mode=share.access_mode,
                allow_export=share.allow_export,
                url=self.share_url(share.share_token),
                matched_key_prefix=key_id_to_prefix.get(share.access_key_id or -1),
                source="KEY",
            )

        for share in known_shares:
            if share.share_token in items_by_token:
                continue
            # 本机记过会话的公开分享也纳入；需密钥分享仅靠有效密钥枚举
            if share.access_mode != ACCESS_MODE_PUBLIC:
                continue
            meta = self._storage.read_meta(share.minute_token) or {}
            title = str(meta.get("title") or share.minute_token)
            items_by_token[share.share_token] = ShareLibraryItemEntity(
                share_token=share.share_token,
                minute_token=share.minute_token,
                title=title,
                access_mode=share.access_mode,
                allow_export=share.allow_export,
                url=self.share_url(share.share_token),
                matched_key_prefix=None,
                source="KNOWN_TOKEN",
            )

        items = sorted(
            items_by_token.values(),
            key=lambda i: (i.title.lower(), i.share_token),
        )
        return ShareLibraryResultEntity(items=items, keys=key_statuses)

    async def resolve_access(
        self,
        share_token: str,
        session_id: str | None,
        *,
        action: str,
        ip: str | None = None,
        user_agent: str | None = None,
        require_export: bool = False,
    ) -> ShareEntity:
        share = await self._require_active_share(share_token)
        if require_export and not share.allow_export:
            raise PermissionError("该分享不允许导出")

        if share.access_mode == ACCESS_MODE_PUBLIC:
            return share

        session = share_session_store.get(session_id)
        if session is None or session.share_token != share_token:
            raise PermissionError("请先使用密钥解锁分享")

        if action in {ACTION_EXPORT, ACTION_MEDIA} and share.access_key_id is not None:
            await self._log_action(
                access_key_id=share.access_key_id,
                share_id=share.id,  # type: ignore[arg-type]
                minute_token=share.minute_token,
                action=action,
                ip=ip,
                user_agent=user_agent,
            )
        return share

    async def _log_action(
        self,
        *,
        access_key_id: int,
        share_id: int,
        minute_token: str,
        action: str,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        async with UnitOfWork() as uow:
            assert uow.share_access_logs is not None
            await uow.share_access_logs.create(
                ShareAccessLogCreateEntity(
                    access_key_id=access_key_id,
                    share_id=share_id,
                    minute_token=minute_token,
                    action=action,
                    ip=ip,
                    user_agent=user_agent,
                    created_at=utc_now_ms(),
                )
            )
            await uow.commit()

    async def _require_active_share(self, share_token: str) -> ShareEntity:
        share = await self.get_by_token(share_token)
        if share is None or share.revoked_at is not None:
            raise LookupError("分享不存在或已失效")
        return share


share_service = ShareService()
