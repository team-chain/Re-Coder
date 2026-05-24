"""
CLI 진입점.

사용:
    cd C:\\ReCoder\\Re-Coder\\core
    python -m eval.v10
    python -m eval.v10 --json report.json --min-pass-rate 0.95
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from .gate import run_v10_gate
from .runner import run_v10_eval


def _to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "value") and obj.__class__.__module__ != "builtins":
        return obj.value
    return obj


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ReCoder v10 Backbone Eval Harness (§38).")
    p.add_argument("--json", type=Path, help="결과를 JSON 파일로 저장")
    p.add_argument("--min-pass-rate", type=float, default=0.95,
                   help="전체 weighted_pass_rate 임계값 (기본 0.95)")
    p.add_argument("--min-category-pass-rate", type=float, default=0.80,
                   help="카테고리별 pass_rate 임계값 (기본 0.80)")
    p.add_argument("--allow-exceptions", action="store_true",
                   help="case 예외도 통과로 처리 (디버깅용)")
    args = p.parse_args(argv)

    report = run_v10_eval()
    gate = run_v10_gate(
        report,
        min_pass_rate=args.min_pass_rate,
        min_category_pass_rate=args.min_category_pass_rate,
        allow_exceptions=args.allow_exceptions,
    )

    # 콘솔 출력 (한 줄씩, ASCII safe)
    print("=" * 70)
    print(f"ReCoder v10 Backbone Eval — total={report.total} "
          f"passed={report.passed} failed={report.failed}")
    print(f"  pass_rate           : {report.pass_rate:.2%}")
    print(f"  weighted_pass_rate  : {report.weighted_pass_rate:.2%}")
    print(f"  safety_violations   : {report.safety_violations}")
    print("-" * 70)
    for cat, info in report.by_category.items():
        print(f"  {cat:30s} {info['passed']:2d}/{info['total']:2d}  "
              f"pass_rate={info['pass_rate']:.2%}")
    print("-" * 70)
    if not gate.passed:
        print(f"[GATE FAIL] reasons:")
        for r in gate.reasons:
            print(f"  - {r}")
    else:
        print(f"[GATE PASS] weighted_pass_rate={gate.weighted_pass_rate:.2%}")
    print("=" * 70)

    # JSON 출력
    if args.json:
        out = {
            "report": _to_jsonable(report),
            "gate":   dataclasses.asdict(gate),
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(out, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"JSON saved: {args.json}")

    return 0 if gate.passed else 1


if __name__ == "__main__":
    sys.exit(main())
