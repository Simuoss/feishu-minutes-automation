"""对图片指定区域盖马赛克（像素化）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass(frozen=True)
class MosaicRegion:
    """归一化矩形：原点左上，取值 [0, 1]。"""

    x1: float
    y1: float
    x2: float
    y2: float
    label: str = ""


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalize_region(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    label: str = "",
    pad: float = 0.02,
) -> MosaicRegion | None:
    """整理并轻微外扩区域；无效矩形返回 None。"""
    left = _clamp01(min(x1, x2))
    top = _clamp01(min(y1, y2))
    right = _clamp01(max(x1, x2))
    bottom = _clamp01(max(y1, y2))
    if right - left < 0.005 or bottom - top < 0.005:
        return None
    if pad > 0:
        left = _clamp01(left - pad)
        top = _clamp01(top - pad)
        right = _clamp01(right + pad)
        bottom = _clamp01(bottom + pad)
    return MosaicRegion(x1=left, y1=top, x2=right, y2=bottom, label=label)


def apply_mosaic(
    image: Image.Image,
    regions: list[MosaicRegion],
    *,
    block_size: int = 12,
) -> Image.Image:
    """在副本上对每个区域做像素化打码。"""
    if not regions:
        return image.copy()

    block = max(2, int(block_size))
    result = image.copy()
    width, height = result.size
    if width <= 0 or height <= 0:
        return result

    for region in regions:
        left = int(region.x1 * width)
        top = int(region.y1 * height)
        right = int(region.x2 * width)
        bottom = int(region.y2 * height)
        left = max(0, min(width, left))
        right = max(0, min(width, right))
        top = max(0, min(height, top))
        bottom = max(0, min(height, bottom))
        if right - left < 2 or bottom - top < 2:
            continue
        crop = result.crop((left, top, right, bottom))
        small_w = max(1, (right - left) // block)
        small_h = max(1, (bottom - top) // block)
        mosaicked = crop.resize((small_w, small_h), Image.Resampling.NEAREST).resize(
            crop.size, Image.Resampling.NEAREST
        )
        result.paste(mosaicked, (left, top))
    return result


def mosaic_file(
    source: Path,
    dest: Path,
    regions: list[MosaicRegion],
    *,
    block_size: int = 12,
    quality: int = 90,
) -> None:
    """读取 source，打码后写入 dest（可与 source 相同以覆盖）。"""
    with Image.open(source) as img:
        rgb = img.convert("RGB")
        redacted = apply_mosaic(rgb, regions, block_size=block_size)
        dest.parent.mkdir(parents=True, exist_ok=True)
        redacted.save(dest, format="JPEG", quality=quality, optimize=True)
