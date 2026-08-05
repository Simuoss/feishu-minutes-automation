"""筛出 basedpyright 输出里的 error 级问题，避开满屏的严格模式 warning。"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    targets = sys.argv[1:] or ["app"]
    proc = subprocess.run(
        [sys.executable, "-m", "basedpyright", "--outputjson", *targets],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("basedpyright 未输出合法 JSON，怀疑调用参数有误：")
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        return 2

    errors = [d for d in payload["generalDiagnostics"] if d["severity"] == "error"]
    for item in errors:
        rel = Path(item["file"]).relative_to(ROOT)
        line = item["range"]["start"]["line"] + 1
        first = item["message"].splitlines()[0]
        print(f"{rel}:{line} {first}")
    print(f"\n共 {len(errors)} 个 error")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
