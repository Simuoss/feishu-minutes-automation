"""说话人编号到真名的映射。

转写文件与纪要正文里存的都是「说话人1」这类会议内稳定编号，真名只存在声纹库里。
所有对外出口（转写页、纪要展示与导出、划词提问、参会人筛选）都经过这里换名，
所以超管改一次名，历史会议立刻跟着变，不需要回写任何文件。

注意别把换名塞进 read_summary_async：R2 同步与仅脱敏那两条路会把读到的正文原样
存回磁盘，一旦读出来的是真名，编号就永久丢了，以后再改名也换不回来。
"""

from __future__ import annotations

import logging

from app.repository.uow import UnitOfWork
from app.service.asr_transcript import (
    LOCAL_LABEL_RE,
    apply_display_names,
    apply_display_names_everywhere,
)

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


async def _names_for(minute_token: str, *, owner_user_id: int) -> dict[str, str]:
    """查这场会议的编号到真名映射；查不到只影响显示，不该挡住正文。"""
    try:
        return await speaker_name_map(minute_token, owner_user_id=owner_user_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "读取说话人命名失败 token=%s，正文将保留编号显示。err=%s",
            minute_token,
            exc,
        )
        return {}


async def apply_speaker_names(
    transcript: str, minute_token: str, *, owner_user_id: int
) -> str:
    """转写正文里把编号换成真名；没有命名过的原样保留。"""
    if not transcript or not LOCAL_LABEL_RE.pattern:
        return transcript
    # 没有编号就不必查库，飞书原文走的就是这条捷径
    if "说话人" not in transcript:
        return transcript
    names = await _names_for(minute_token, owner_user_id=owner_user_id)
    return apply_display_names(transcript, names)


async def apply_speaker_names_to_summary(
    content: str, minute_token: str, *, owner_user_id: int
) -> str:
    """纪要正文里把编号换成真名。

    纪要是散文，编号可能出现在任何位置，所以不像转写那样只认段首。提示词要求
    模型称呼参会人时原样照抄转写里的标识，正是为了让这一步替换得干净。
    """
    if not content or "说话人" not in content:
        return content
    names = await _names_for(minute_token, owner_user_id=owner_user_id)
    return apply_display_names_everywhere(content, names)
