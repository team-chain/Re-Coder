#!/usr/bin/env python3
"""
ReCoder — AWS 계정 교체 도구.

새 AWS 계정으로 갈아탈 때, 흩어진 설정 대신 이 스크립트 한 번으로:
  1) core/.env 의 리전 3종(AWS_REGION / AWS_DEFAULT_REGION / BEDROCK_REGION)을 동일하게 설정
  2) (선택) AWS_PROFILE / Bedrock 모델 ID 갱신
  3) ~/.recoder/diagnostics.json 캐시 삭제 → 다음 실행 때 새 계정으로 재진단

자격증명(access key/secret)은 코드에 저장하지 않는다. `aws configure` 또는
~/.aws/credentials 프로필로 관리하고, 여기서는 그 프로필 이름만 가리킨다.

사용 예:
  python switch_aws.py --region ap-northeast-2
  python switch_aws.py --region us-east-1 --profile recoder
  python switch_aws.py --region us-east-1 --primary-model anthropic.claude-3-5-sonnet-20241022-v2:0
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_ENV = HERE / ".env"

# 리전을 바꾸면 동일 값으로 세팅되는 키들 (모듈마다 읽는 변수명이 달라 3종 모두 맞춘다)
REGION_KEYS = ["AWS_REGION", "AWS_DEFAULT_REGION", "BEDROCK_REGION"]


def load_env(path: Path) -> "list[tuple[str, str] | str]":
    """라인 단위로 읽되 KEY=VAL 은 (key,val), 그 외(주석/빈줄)는 원문 보존."""
    if not path.exists():
        return []
    items = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            k, _, v = raw.partition("=")
            items.append((k.strip(), v))
        else:
            items.append(raw)
    return items


def write_env(path: Path, items, managed: dict) -> None:
    seen = set()
    out_lines = []
    for it in items:
        if isinstance(it, tuple):
            k, v = it
            if k in managed:
                out_lines.append(f"{k}={managed[k]}")
                seen.add(k)
            else:
                out_lines.append(f"{k}={v}")
        else:
            out_lines.append(it)
    missing = [k for k in managed if k not in seen]
    if missing:
        out_lines.append("")
        out_lines.append("# ── AWS 계정 설정 (switch_aws.py 가 관리) ──")
        for k in missing:
            out_lines.append(f"{k}={managed[k]}")
    # BOM 없이 LF 로 저장
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="ReCoder AWS 계정 교체")
    ap.add_argument("--region", required=True, help="새 AWS 리전 (예: us-east-1, ap-northeast-2)")
    ap.add_argument("--profile", help="~/.aws/credentials 의 프로필 이름 (선택)")
    ap.add_argument("--primary-model", help="BEDROCK_PRIMARY_MODEL_IDENTIFIER (선택)")
    ap.add_argument("--secondary-model", help="BEDROCK_SECONDARY_MODEL_IDENTIFIER (선택)")
    ap.add_argument("--fast-model", help="BEDROCK_FAST_MODEL_IDENTIFIER (선택)")
    ap.add_argument("--env", default=str(DEFAULT_ENV), help="대상 .env 경로")
    ap.add_argument("--keep-diagnostics", action="store_true", help="diagnostics.json 캐시 삭제 생략")
    args = ap.parse_args()

    managed: dict[str, str] = {k: args.region for k in REGION_KEYS}
    if args.profile:
        managed["AWS_PROFILE"] = args.profile
    if args.primary_model:
        managed["BEDROCK_PRIMARY_MODEL_IDENTIFIER"] = args.primary_model
    if args.secondary_model:
        managed["BEDROCK_SECONDARY_MODEL_IDENTIFIER"] = args.secondary_model
    if args.fast_model:
        managed["BEDROCK_FAST_MODEL_IDENTIFIER"] = args.fast_model

    env_path = Path(args.env)
    items = load_env(env_path)
    write_env(env_path, items, managed)
    print(f"[OK] {env_path} 업데이트:")
    for k, v in managed.items():
        print(f"     {k}={v}")

    # 진단 캐시 삭제 (새 계정으로 재진단되도록)
    if not args.keep_diagnostics:
        diag = Path(os.environ.get("RECODER_HOME", str(Path.home() / ".recoder"))) / "diagnostics.json"
        if diag.exists():
            diag.unlink()
            print(f"[OK] 진단 캐시 삭제: {diag}")
        else:
            print(f"[..] 진단 캐시 없음(건너뜀): {diag}")

    print("\n다음 단계:")
    print("  1) 새 계정 자격증명 설정:  aws configure" + (f" --profile {args.profile}" if args.profile else ""))
    print("  2) 새 계정 Bedrock 콘솔에서 사용할 모델 액세스 활성화 (리전: %s)" % args.region)
    print("  3) Local Core 재시작:  python main.py")
    print("  4) (게이트웨이 쓰는 경우) gateway 재배포:  cd ../gateway && sam deploy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
