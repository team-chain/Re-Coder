#!/usr/bin/env python3
"""릴리스 태그와 제품 버전이 같은지 확인한다.

태그 push(`v2.0.23`, 시험용 `v2.0.23-rc1`)로 릴리스를 빌드할 때 가장 먼저 돈다.
태그만 올리고 버전 올리기를 잊으면 "2.0.22 파일이 v2.0.23 이름으로 배포"되는 사고가
난다. 그 전에 멈춘다.

  python scripts/check_release_tag.py v2.0.23        # 인자로
  GITHUB_REF_NAME=v2.0.23 python scripts/check_release_tag.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)(?:-(rc\d+))?$")


def versions(root: Path = ROOT) -> dict[str, str]:
    ext = json.loads((root / "extension" / "package.json").read_text(encoding="utf-8"))["version"]
    lock = json.loads((root / "extension" / "package-lock.json").read_text(encoding="utf-8"))["version"]
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', (root / "core" / "version.py").read_text(encoding="utf-8"), re.M)
    return {"extension/package.json": ext, "extension/package-lock.json": lock,
            "core/version.py": m.group(1) if m else ""}


def check(tag: str, root: Path = ROOT) -> tuple[bool, str, bool]:
    """(통과 여부, 메시지, 시험 태그 여부)."""
    m = TAG_RE.match(tag.strip())
    if not m:
        return False, f"태그 형식이 아닙니다: {tag!r} (예: v2.0.23, v2.0.23-rc1)", False
    want, rc = m.group(1), bool(m.group(2))
    found = versions(root)
    bad = {k: v for k, v in found.items() if v != want}
    if bad:
        detail = ", ".join(f"{k}={v}" for k, v in bad.items())
        return False, f"태그 {tag} 와 버전이 다릅니다: {detail}. 버전을 {want} 로 올린 커밋에 태그를 다세요.", rc
    return True, f"태그 {tag} = 버전 {want}" + (" (시험 태그)" if rc else ""), rc


def main(argv: list[str]) -> int:
    tag = argv[1] if len(argv) > 1 else os.environ.get("GITHUB_REF_NAME", "")
    ok, msg, rc = check(tag)
    print(msg)
    out = os.environ.get("GITHUB_OUTPUT")
    if ok and out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"prerelease={'true' if rc else 'false'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
