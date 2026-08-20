"""区分「没提这个字段」和「把这个字段置空」。

更新用的实体把一张表的字段都摆出来，调用方只填想改的那几个，剩下的靠仓储跳过。
以前「没填」就是 None，于是「置空」这件事没法表达——写 None 进去是个静默的空操作，
不报错也不生效，转写来源置空就是这么丢掉的。

所以默认值改成 UNSET：不填就是不改，显式写 None 才是置空。
"""

from __future__ import annotations

from enum import Enum
from typing import TypeAlias, TypeVar


class Unset(Enum):
    """单成员枚举，这样类型检查器能把 UNSET 当成一个可收窄的字面量。"""

    token = "UNSET"

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = Unset.token

_T = TypeVar("_T")

# Maybe[str] 读作：可能没提、可能是 str、也可能是 None（置空）
Maybe: TypeAlias = _T | None | Unset


def is_set(value: object) -> bool:
    """这个字段提了没有。注意 None 算提了，意思是置空。"""
    return not isinstance(value, Unset)
