from sqlalchemy import (
    BigInteger,
    Float,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_base import Base


class VoiceprintORM(Base):
    """全局人物库：一行是一个人，不分归属用户。

    跨租户共享是有意为之——同一个人在不同管理员的会议里应当认得出来；
    作为代价，命名权只给超管。
    """

    __tablename__ = "voiceprints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # float32 小端序拼成的质心向量
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    dim: Mapped[int] = mapped_column(Integer)
    # 参与质心的样本段数，用作加权平均的权重
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    meeting_count: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 被合并到哪个人物；非空表示这行已作废，仅留作历史指向
    merged_into: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class VoiceprintSampleORM(Base):
    """人物的可试听片段，指向某场会议音频的一个时间区间。"""

    __tablename__ = "voiceprint_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    voiceprint_id: Mapped[int] = mapped_column(Integer, index=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, index=True)
    minute_token: Mapped[str] = mapped_column(String(32), index=True)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    # 该片段与人物质心的相似度，排序时优先给最像的
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger)


class MeetingSpeakerORM(Base):
    """一场会议里的说话人：本地编号、云端分离出的 ID、以及对应的人物。"""

    __tablename__ = "meeting_speakers"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "minute_token",
            "local_label",
            name="uq_meeting_speakers_owner_token_label",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, index=True)
    minute_token: Mapped[str] = mapped_column(String(32), index=True)
    # 写进转写文件里的稳定编号，如「说话人1」
    local_label: Mapped[str] = mapped_column(String(64))
    # 云端分离给的 ID 列表（切段后同一个人可能对应多个），JSON 数组
    cloud_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    voiceprint_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    talk_ms: Mapped[int] = mapped_column(Integer, default=0)
    segments: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)
