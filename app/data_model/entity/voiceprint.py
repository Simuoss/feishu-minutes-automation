from dataclasses import dataclass, field


@dataclass
class VoiceprintEntity:
    id: int
    display_name: str | None
    embedding: bytes
    dim: int
    sample_count: int
    meeting_count: int
    note: str | None
    merged_into: int | None
    created_at: int
    updated_at: int

    @property
    def named(self) -> bool:
        return bool((self.display_name or "").strip())


@dataclass
class VoiceprintCreateEntity:
    embedding: bytes
    dim: int
    sample_count: int = 0
    meeting_count: int = 0
    display_name: str | None = None
    note: str | None = None


@dataclass
class VoiceprintUpdateEntity:
    id: int
    display_name: str | None = None
    embedding: bytes | None = None
    sample_count: int | None = None
    meeting_count: int | None = None
    note: str | None = None
    merged_into: int | None = None


@dataclass
class VoiceprintSampleEntity:
    id: int
    voiceprint_id: int
    owner_user_id: int
    minute_token: str
    start_ms: int
    end_ms: int
    score: float | None
    created_at: int


@dataclass
class VoiceprintSampleCreateEntity:
    voiceprint_id: int
    owner_user_id: int
    minute_token: str
    start_ms: int
    end_ms: int
    score: float | None = None


PROPOSAL_PENDING = "PENDING"
PROPOSAL_APPROVED = "APPROVED"
PROPOSAL_REJECTED = "REJECTED"


@dataclass
class ProposalSample:
    start_ms: int
    end_ms: int
    score: float | None = None


@dataclass
class VoiceprintNameProposalEntity:
    id: int
    owner_user_id: int
    minute_token: str
    proposed_name: str
    voiceprint_id: int | None
    embedding: bytes
    dim: int
    score: float | None
    sample_count: int
    samples: list[ProposalSample]
    status: str
    created_at: int
    decided_at: int | None


@dataclass
class VoiceprintNameProposalCreateEntity:
    owner_user_id: int
    minute_token: str
    proposed_name: str
    embedding: bytes
    dim: int
    voiceprint_id: int | None = None
    score: float | None = None
    sample_count: int = 0
    samples: list[ProposalSample] = field(default_factory=list)


@dataclass
class MeetingSpeakerEntity:
    id: int
    owner_user_id: int
    minute_token: str
    local_label: str
    cloud_ids: list[str]
    voiceprint_id: int | None
    match_score: float | None
    talk_ms: int
    segments: int
    created_at: int
    updated_at: int


@dataclass
class MeetingSpeakerUpsertEntity:
    owner_user_id: int
    minute_token: str
    local_label: str
    cloud_ids: list[str] = field(default_factory=list)
    voiceprint_id: int | None = None
    match_score: float | None = None
    talk_ms: int = 0
    segments: int = 0
