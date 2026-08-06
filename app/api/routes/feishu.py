import logging

from fastapi import APIRouter, HTTPException, Request

from app.api.assembler.meeting_record_assembler import meeting_record_entity_to_response
from app.core.auth_context import owner_scope_user_id, require_auth
from app.data_model.entity.meeting_record import MeetingRecordQueryEntity
from app.dto.feishu.meeting_record import HealthResponse, MeetingRecordResponse
from app.repository.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@router.get("/records", response_model=list[MeetingRecordResponse])
async def list_records(
    request: Request, status: str | None = None
) -> list[MeetingRecordResponse]:
    require_auth(request)
    owner_scope = owner_scope_user_id(request)
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        entities = await uow.meeting_records.query(
            MeetingRecordQueryEntity(status=status, owner_user_id=owner_scope)
        )
    return [meeting_record_entity_to_response(e) for e in entities]


@router.get("/records/{record_id}", response_model=MeetingRecordResponse)
async def get_record(record_id: int, request: Request) -> MeetingRecordResponse:
    require_auth(request)
    owner_scope = owner_scope_user_id(request)
    async with UnitOfWork() as uow:
        assert uow.meeting_records is not None
        entity = await uow.meeting_records.get_by_id(record_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    if owner_scope is not None and entity.owner_user_id != owner_scope:
        raise HTTPException(status_code=404, detail="记录不存在")
    return meeting_record_entity_to_response(entity)
