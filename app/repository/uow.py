from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.repository.access_key_repository import AccessKeyRepository
from app.repository.feishu_user_token_repository import FeishuUserTokenRepository
from app.repository.figure_repository import FigureRepository
from app.repository.meeting_record_repository import MeetingRecordRepository
from app.repository.pipeline_job_repository import PipelineJobRepository
from app.repository.r2_sync_state_repository import R2SyncStateRepository
from app.repository.recording_session_repository import RecordingSessionRepository
from app.repository.redaction_figure_repository import RedactionFigureRepository
from app.repository.share_access_log_repository import ShareAccessLogRepository
from app.repository.share_repository import ShareRepository
from app.repository.summary_run_repository import SummaryRunRepository
from app.repository.user_repository import UserRepository


class UnitOfWork:
    def __init__(self) -> None:
        self._session: AsyncSession | None = None
        self.meeting_records: MeetingRecordRepository | None = None
        self.recording_sessions: RecordingSessionRepository | None = None
        self.access_keys: AccessKeyRepository | None = None
        self.shares: ShareRepository | None = None
        self.share_access_logs: ShareAccessLogRepository | None = None
        self.users: UserRepository | None = None
        self.summary_runs: SummaryRunRepository | None = None
        self.figures: FigureRepository | None = None
        self.redaction_figures: RedactionFigureRepository | None = None
        self.feishu_user_tokens: FeishuUserTokenRepository | None = None
        self.pipeline_jobs: PipelineJobRepository | None = None
        self.r2_sync_states: R2SyncStateRepository | None = None

    async def __aenter__(self) -> Self:
        self._session = async_session_factory()
        self.meeting_records = MeetingRecordRepository(self._session)
        self.recording_sessions = RecordingSessionRepository(self._session)
        self.access_keys = AccessKeyRepository(self._session)
        self.shares = ShareRepository(self._session)
        self.share_access_logs = ShareAccessLogRepository(self._session)
        self.users = UserRepository(self._session)
        self.summary_runs = SummaryRunRepository(self._session)
        self.figures = FigureRepository(self._session)
        self.redaction_figures = RedactionFigureRepository(self._session)
        self.feishu_user_tokens = FeishuUserTokenRepository(self._session)
        self.pipeline_jobs = PipelineJobRepository(self._session)
        self.r2_sync_states = R2SyncStateRepository(self._session)
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
