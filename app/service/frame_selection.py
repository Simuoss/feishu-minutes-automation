"""从一簇候选帧里挑出最值得配图的那一张，并跨图去重。

讲话往往滞后于画面，所以每个候选时间点会抽若干帧；这里用纯图像特征做三件事：
剔掉黑屏/纯色的过渡帧、优先挑信息密度高（文字多、清晰）的帧、
再用感知哈希把和已选图几乎一样的丢掉——一页幻灯片能停好几分钟，
不去重的话文档里会出现十几张一模一样的截图。
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

logger = logging.getLogger(__name__)

# 灰度标准差低于此值基本是纯色/黑屏过渡帧
MIN_TONE_STDDEV = 8.0
# 边缘能量低于此值说明画面几乎没有内容（空白桌面、纯色背景）
MIN_EDGE_ENERGY = 3.0

# 哈希精度按实测定：8x8 对录屏完全不够用——大面积留白加细小文字，
# 会把相隔十几分钟的不同页面压成同一个指纹。16x16 下实测结果是
# 同一画面相距 0~4%，不同页面相距 28~50%，两者截然分开。
_HASH_SIZE = 16
HASH_BITS = _HASH_SIZE * _HASH_SIZE
# 取 9.4%，落在上述两档之间
DUPLICATE_HAMMING = 24


@dataclass
class FrameStats:
    seconds: float
    path: Path
    tone_stddev: float
    edge_energy: float
    fingerprint: int

    @property
    def is_blank(self) -> bool:
        return self.tone_stddev < MIN_TONE_STDDEV or self.edge_energy < MIN_EDGE_ENERGY

    @property
    def score(self) -> float:
        """信息密度：录屏里文字越多、越清晰，边缘能量越高。"""
        return self.edge_energy


def _dhash(image: Image.Image) -> int:
    resized = image.resize((_HASH_SIZE + 1, _HASH_SIZE), Image.Resampling.LANCZOS)
    # 灰度图 tobytes 就是逐行排列的像素值，比 getdata 更直接
    pixels = resized.tobytes()
    bits = 0
    for row in range(_HASH_SIZE):
        offset = row * (_HASH_SIZE + 1)
        for col in range(_HASH_SIZE):
            left = pixels[offset + col]
            right = pixels[offset + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def analyze_frame(path: Path, seconds: float) -> FrameStats | None:
    """读取一帧并计算特征，读不出来返回 None。"""
    try:
        with Image.open(path) as raw:
            gray = raw.convert("L")
            tone_stddev = ImageStat.Stat(gray).stddev[0]
            # 缩到固定宽度再算边缘，避免不同分辨率之间不可比
            small = gray.resize((320, max(1, int(320 * gray.height / gray.width))))
            edges = small.filter(ImageFilter.FIND_EDGES)
            edge_energy = ImageStat.Stat(edges).stddev[0]
            fingerprint = _dhash(gray)
    except (OSError, ValueError) as exc:
        logger.warning(
            "候选帧无法解析 path=%s，怀疑抽帧时写入被截断，已跳过该帧: %s",
            path,
            exc,
        )
        return None

    return FrameStats(
        seconds=seconds,
        path=path,
        tone_stddev=tone_stddev,
        edge_energy=edge_energy,
        fingerprint=fingerprint,
    )


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def is_duplicate(
    fingerprint: int,
    seen: list[int],
    *,
    threshold: int = DUPLICATE_HAMMING,
) -> bool:
    return any(hamming_distance(fingerprint, other) <= threshold for other in seen)


def pick_best_frame(candidates: list[FrameStats]) -> FrameStats | None:
    """从一簇帧里挑最好的一张：先排除空白帧，再按信息密度取最高。"""
    usable = [c for c in candidates if not c.is_blank]
    if not usable:
        return None
    return max(usable, key=lambda c: c.score)


def select_distinct_frames(
    groups: list[list[FrameStats]],
    *,
    limit: int | None = None,
    threshold: int = DUPLICATE_HAMMING,
) -> list[FrameStats]:
    """按顺序为每组挑一帧，跳过与已选画面重复的组。"""
    selected: list[FrameStats] = []
    seen: list[int] = []

    for group in groups:
        if limit is not None and len(selected) >= limit:
            break
        best = pick_best_frame(group)
        if best is None:
            continue
        if is_duplicate(best.fingerprint, seen, threshold=threshold):
            logger.info(
                "跳过 %.1fs 的候选帧：与已选画面几乎相同，配图重复对复习没有价值",
                best.seconds,
            )
            continue
        selected.append(best)
        seen.append(best.fingerprint)

    return selected
