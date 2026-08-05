from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.repository.access_key_repository import AccessKeyRepository
from app.repository.meeting_record_repository import MeetingRecordRepository
from app.repository.recording_session_repository import RecordingSessionRepository
from app.repository.share_access_log_repository import ShareAccessLogRepository
from app.repository.share_repository import ShareRepository


class UnitOfWork:
    def __init__(self) -> None:
        self._session: AsyncSession | None = None
        self.meeting_records: MeetingRecordRepository | None = None
        self.recording_sessions: RecordingSessionRepository | None = None
        self.access_keys: AccessKeyRepository | None = None
        self.shares: ShareRepository | None = None
        self.share_access_logs: ShareAccessLogRepository | None = None

    async def __aenter__(self) -> Self:
        self._session = async_session_factory()
        self.meeting_records = MeetingRecordRepository(self._session)
        self.recording_sessions = RecordingSessionRepository(self._session)
        self.access_keys = AccessKeyRepository(self._session)
        self.shares = ShareRepository(self._session)
        self.share_access_logs = ShareAccessLogRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is not None:
            await self._session.rollback()
        await self._session.close()

    async def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("UnitOfWork 未初始化")
        await self._session.commit()

    async def rollback(self) -> None:
        if self._session is None:
            raise RuntimeError("UnitOfWork 未初始化")
        await self._session.rollback()
