#!/usr/bin/env python3
"""
학생 토큰 일괄 발급 스크립트 (운영자 로컬에서 실행).

게이트웨이 /admin 엔드포인트를 호출해 학생 토큰을 발급하고 CSV 로 저장한다.
발급된 토큰 원문은 이 시점에만 표시되므로 학생에게 안전하게 전달할 것.

사용법:
  python issue_tokens.py \
      --endpoint https://xxxx.execute-api.<region>.amazonaws.com/admin \
      --admin-key <ADMIN_KEY> \
      --students s01,s02,s03 \
      --out tokens.csv

또는 학번 파일(줄당 1명, 선택적으로 'sid,discord_user_id'):
  python issue_tokens.py --endpoint ... --admin-key ... --file students.txt --out tokens.csv
"""
import argparse
import csv
import json
import sys
import urllib.request
import urllib.error


def call_admin(endpoint: str, admin_key: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Admin-Key": admin_key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": e.code, "message": e.read().decode("utf-8", "ignore")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", required=True, help="...amazonaws.com/admin")
    ap.add_argument("--admin-key", required=True)
    ap.add_argument("--students", help="쉼표구분 학번 목록")
    ap.add_argument("--file", help="학번 파일(줄당 1명)")
    ap.add_argument("--out", default="tokens.csv")
    ap.add_argument("--max-total", type=int, default=500000)
    ap.add_argument("--max-daily", type=int, default=100000)
    ap.add_argument("--rpm", type=int, default=10)
    args = ap.parse_args()

    # (student_id, discord_user_id) 목록. discord_user_id 는 선택(1:1 바인딩용).
    entries = []
    if args.students:
        entries += [(s.strip(), "") for s in args.students.split(",") if s.strip()]
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                parts = [p.strip() for p in ln.split(",")]
                entries.append((parts[0], parts[1] if len(parts) > 1 else ""))
    if not entries:
        print("학번이 없습니다. --students 또는 --file 지정.", file=sys.stderr)
        sys.exit(1)

    rows = []
    for sid, discord_id in entries:
        res = call_admin(args.endpoint, args.admin_key, {
            "action": "issue", "student_id": sid, "discord_user_id": discord_id,
            "max_total": args.max_total, "max_daily": args.max_daily, "rpm": args.rpm})
        if "token" in res:
            print(f"  발급: {sid}" + (f" (discord={discord_id})" if discord_id else ""))
            rows.append({"student_id": sid, "discord_user_id": discord_id, "token": res["token"]})
        else:
            print(f"  실패: {sid} -> {res}", file=sys.stderr)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["student_id", "discord_user_id", "token"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n총 {len(rows)}명 발급 → {args.out}")
    print("⚠️ 토큰 원문은 이 파일에만 있습니다. 학생에게 개별 전달 후 안전하게 보관/삭제하세요.")


if __name__ == "__main__":
    main()
