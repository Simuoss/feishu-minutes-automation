"""说话人编号到真名的映射。

转写文件里存的是「说话人1」这类会议内稳定编号，真名只存在声纹库里。所有对外
出口（转写页、纪要生成、划词提问、导出、参会人筛选）都经过这里换名，所以超管
改一次名，历史会议立刻跟着变，不需要回写任何文件。
"""

from __future__ import annotations

import logging

from app.repository.uow import UnitOfWork
from app.service.asr_transcript import LOCAL_LABEL_RE, apply_display_names

logger = logging.getLogger(__name__)


async def speaker_name_map(
    minute_token: str, *, owner_user_id: int
) -> dict[str, str]:
    """返回 {说话人1: 真名}，未命名的人物不出现在结果里。"""
    async with UnitOfWork() as uow:
        assert uow.voiceprints is not None
        speakers = await uow.voiceprints.list_speakers(
            minute_token, owner_user_id=owner_user_id
        )
        names: dict[str, str] = {}
        for speaker in speakers:
            if speaker.voiceprint_id is None:
                continue
            person = await uow.voiceprints.resolve(speaker.voiceprint_id)
            if person is None or not person.named:
                continue
            names[speaker.local_label] = str(person.display_name).strip()
    return names


async def apply_speaker_names(
    transcript: str, minute_token: str, *, owner_user_id: int
) -> str:
    """转写正文里把编号换成真名；没有命名过的原样保留。"""
    if not transcript or not LOCAL_LABEL_RE.pattern:
        return transcript
    # 没有编号就不必查库，飞书原文走的就是这条捷径
    if "说话人" not in transcript:
        return transcript
    try:
        names = await speaker_name_map(minute_token, owner_user_id=owner_user_id)
    except Exception as exc:  # noqa: BLE001 — 查不到名字只影响显示，不该挡住转写
        logger.error(
            "读取说话人命名失败 token=%s，转写将保留编号显示。err=%s",
            minute_token,
            exc,
        )
        return transcript
    return apply_display_names(transcript, names)
