"""声纹：算嵌入、认人、维护全局人物库。

云端识别只保证「一段音频内部谁跟谁是同一个人」，而且长音频要切段送，段与段之间
的 speaker_0/1 对不上号。所以真正的身份由这里给：把每个（切段, 云端说话人）的音频
样本算成向量，先在会议内部聚一次，再去全局库里认人。认出来的挂到已有人物，认不出
的建新条目，等超管命名。

模型用 3D-Speaker 的 CAM++ 中文版，纯 onnxruntime 推理，CPU 上一段 8 秒音频约几十
毫秒，不需要显卡。
"""

from __future__ import annotations

import array
import logging
import math
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core import runtime_config
from app.core.config import settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# 能量低于该值的片段基本是静音或环境噪声，算出来的向量不可信
MIN_RMS = 0.01
# 片段与本组粗质心的相似度低于该值就剔除，防一段串音污染整个人物
OUTLIER_FLOOR = 0.45


class VoiceprintError(RuntimeError):
    """声纹模型不可用。"""


@dataclass
class SpeakerSample:
    start_ms: int
    end_ms: int
    embedding: list[float] = field(default_factory=list)
    rms: float = 0.0


@dataclass
class SpeakerCandidate:
    """一场会议里待认领的说话人：可能由多个云端 ID 合并而来。"""

    cloud_ids: list[str]
    samples: list[SpeakerSample]
    talk_ms: int = 0
    segments: int = 0
    centroid: list[float] = field(default_factory=list)


def encode_embedding(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def decode_embedding(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def mean_vector(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    size = len(vectors[0])
    total = [0.0] * size
    for vector in vectors:
        for i in range(size):
            total[i] += vector[i]
    return [value / len(vectors) for value in total]


def weighted_merge(
    base: list[float], base_weight: int, extra: list[float], extra_weight: int
) -> list[float]:
    """按样本数加权合并两个质心，人物用得越多越稳。"""
    if not base:
        return list(extra)
    if not extra:
        return list(base)
    total = max(1, base_weight + extra_weight)
    return [
        (b * base_weight + e * extra_weight) / total for b, e in zip(base, extra)
    ]


def trimmed_centroid(samples: list[SpeakerSample]) -> list[float]:
    vectors = [s.embedding for s in samples if s.embedding]
    if not vectors:
        return []
    rough = mean_vector(vectors)
    kept = [v for v in vectors if cosine(v, rough) >= OUTLIER_FLOOR]
    return mean_vector(kept) if len(kept) >= 2 else rough


def read_wav_samples(path: Path) -> list[float]:
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise VoiceprintError(f"只支持 16 位 PCM，实际 {handle.getsampwidth() * 8} 位")
        frames = handle.readframes(handle.getnframes())
    pcm = array.array("h")
    pcm.frombytes(frames)
    return [value / 32768.0 for value in pcm]


def rms_of(samples: list[float]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(value * value for value in samples) / len(samples))


class EmbeddingExtractor:
    """包一层 sherpa-onnx，懒加载模型（进程内只加载一次）。"""

    def __init__(self, model_path: str | None = None) -> None:
        self._model_path = model_path or settings.speaker_embedding_model_path
        self._extractor: Any | None = None

    def resolve_model_path(self) -> Path:
        path = Path(self._model_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        return path

    def available(self) -> bool:
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return False
        return self.resolve_model_path().is_file()

    def _ensure(self) -> Any:
        if self._extractor is not None:
            return self._extractor
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise VoiceprintError(
                "未安装 sherpa-onnx，无法计算声纹；请先 pip install sherpa-onnx"
            ) from exc
        model_path = self.resolve_model_path()
        if not model_path.is_file():
            raise VoiceprintError(
                f"声纹模型文件不存在: {model_path}；"
                "请跑 python scripts/download_speaker_model.py 下载"
            )
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path), num_threads=2, provider="cpu"
        )
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        logger.info("声纹模型已加载 path=%s dim=%s", model_path, self._extractor.dim)
        return self._extractor

    @property
    def dim(self) -> int:
        return int(self._ensure().dim)

    def embed_file(self, wav_path: Path) -> tuple[list[float], float]:
        samples = read_wav_samples(wav_path)
        energy = rms_of(samples)
        if energy < MIN_RMS:
            return [], energy
        extractor = self._ensure()
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=samples)
        stream.input_finished()
        return list(extractor.compute(stream)), energy


extractor = EmbeddingExtractor()


def match_threshold() -> float:
    return runtime_config.get_float(
        "SPEAKER_MATCH_THRESHOLD", settings.speaker_match_threshold
    )


def cluster_candidates(candidates: list[SpeakerCandidate]) -> list[SpeakerCandidate]:
    """把会议内互相像的说话人并成一个人。

    两种情况都要靠它：切段导致同一个人在不同段里编号不同；以及云端在同一段内把一个
    人拆成了好几个。合并用的阈值比认人稍高一点，宁可留两个候选让全局库去认。
    """
    threshold = match_threshold() + 0.1
    merged: list[SpeakerCandidate] = []
    for candidate in candidates:
        if not candidate.centroid:
            merged.append(candidate)
            continue
        target = None
        best = 0.0
        for existing in merged:
            if not existing.centroid:
                continue
            score = cosine(existing.centroid, candidate.centroid)
            if score >= threshold and score > best:
                target, best = existing, score
        if target is None:
            merged.append(candidate)
            continue
        logger.info(
            "会议内说话人合并 %s <- %s 相似度 %.3f",
            target.cloud_ids,
            candidate.cloud_ids,
            best,
        )
        target.cloud_ids.extend(candidate.cloud_ids)
        target.samples.extend(candidate.samples)
        target.talk_ms += candidate.talk_ms
        target.segments += candidate.segments
        target.centroid = trimmed_centroid(target.samples)
    merged.sort(key=lambda c: c.talk_ms, reverse=True)
    return merged
