"""下载声纹嵌入模型（sherpa-onnx 用的 onnx 文件）。

模型有几十兆，不进仓库，部署时跑一次即可：

    python scripts/download_speaker_model.py

默认拉 3D-Speaker 的 CAM++ 中文模型。实测在本项目的会议录音上，同一个人跨会议的
质心相似度最低 0.68，不同人最高 0.42，区分度好于同系列的 eres2net，且体积更小。
GitHub Releases 在国内常连不上，所以默认走 HuggingFace 镜像，可用 --source 切换。
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"

SOURCES = {
    "hf-mirror": (
        "https://hf-mirror.com/csukuangfj/speaker-embedding-models/resolve/main"
    ),
    "huggingface": (
        "https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/main"
    ),
    "github": (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=sorted(SOURCES), default="hf-mirror")
    parser.add_argument("--model", default=MODEL_NAME)
    parser.add_argument("--dest", default=str(ROOT / "models"))
    args = parser.parse_args()

    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / args.model
    if dest.is_file() and dest.stat().st_size > 0:
        print(f"已存在，跳过：{dest}（{dest.stat().st_size / 1024 / 1024:.1f}MB）")
        return 0

    url = f"{SOURCES[args.source]}/{args.model}"
    print(f"下载 {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as fh:
            while chunk := response.read(1 << 20):
                fh.write(chunk)
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        print(f"下载失败：{exc}\n可换 --source huggingface 或 --source github 重试")
        return 1

    tmp.replace(dest)
    print(f"完成：{dest}（{dest.stat().st_size / 1024 / 1024:.1f}MB）")
    print("如放在别处，记得同步改 .env 的 SPEAKER_EMBEDDING_MODEL_PATH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
