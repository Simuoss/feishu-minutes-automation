from app.data_model.entity.meeting_record import MeetingRecordEntity
from app.dto.feishu.meeting_record import MeetingRecordResponse


def meeting_record_entity_to_response(entity: MeetingRecordEntity) -> MeetingRecordResponse:
    return MeetingRecordResponse(
        id=entity.id or 0,
        feishu_event_id=entity.feishu_event_id,
        event_type=entity.event_type,
        unique_key=entity.unique_key,
        minute_token=entity.minute_token,
        meeting_id=entity.meeting_id,
        title=entity.title,
        duration_ms=entity.duration_ms,
        has_video=entity.has_video,
        status=entity.status,
        error_message=entity.error_message,
        storage_path=entity.storage_path,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )
