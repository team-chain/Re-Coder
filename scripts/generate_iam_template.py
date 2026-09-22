#!/usr/bin/env python3
"""infra/recoder-iam-quickcreate.json 재생성 — 정책의 원본은 core/aws_policy.py 다.

aws_policy 가 바뀌면 이 스크립트를 다시 돌려 커밋한다. 파일과 코드가
어긋나면 core/tests/test_aws_onboarding.py 의 드리프트 검사가 실패한다.

    python3 scripts/generate_iam_template.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

import aws_onboarding  # noqa: E402

OUT = ROOT / "infra" / "recoder-iam-quickcreate.json"

def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(aws_onboarding.template_json(), encoding="utf-8")
    print(f"쓴 파일: {OUT}")

if __name__ == "__main__":
    main()
