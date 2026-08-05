"""删除已迁入 DB、不再需要的本地元数据文件（保留正文/媒体/转写/配图二进制）。

用法（项目根目录）:
  python scripts/cleanup_filesystem_meta.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings

TARGETS = (
    "meta.json",
    "output/summary.meta.json",
    "output/r2.meta.json",
    "agent/figures.manifest.json",
    "agent/redaction/audit.json",
)

TOKEN_FILE = Path("./data/feishu_user_token.json")


def main() -> int:
    root = Path(settings.storage_root)
    deleted = 0
    missing = 0
    if root.is_dir():
        for meeting_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for rel in TARGETS:
                path = meeting_dir / rel
                if path.is_file():
                    path.unlink()
                    deleted += 1
                    print(f"deleted {path}")
                else:
                    missing += 1
    if TOKEN_FILE.is_file():
        TOKEN_FILE.unlink()
        deleted += 1
        print(f"deleted {TOKEN_FILE}")
    print(f"done deleted={deleted} already_absent≈{missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
