#!/usr/bin/env python3
"""AWS Budgets 예산 알람 셋업 — 보드 카드 「롤백 1회 리허설 + 예산 알람(Budgets)」.

켜 둔 채 잊힌 배포는 화면에 아무 신호도 없다 — 청구서가 첫 신호가 되면
늦다. 월 예산과 임계(80%/100%) 이메일 알람을 한 번에 만든다.

사용:
    python3 scripts/setup_budget.py --email you@example.com            # 기본 $15/월
    python3 scripts/setup_budget.py --email you@example.com --limit 30
    python3 scripts/setup_budget.py --email you@example.com --dry-run  # 페이로드만 출력

권한: budgets:CreateBudget / budgets:DescribeBudget (aws_policy 대상 밖 —
온보딩 사용자 키가 아니라 **본인 콘솔 계정**으로 한 번 실행하는 관리 작업이다).
"""
from __future__ import annotations

import argparse
import json
import sys

BUDGET_NAME = "recoder-monthly"


def build_payloads(email: str, limit_usd: float) -> tuple[dict, list[dict]]:
    """CreateBudget 페이로드 — 테스트가 이 함수를 고정한다."""
    if limit_usd <= 0:
        raise ValueError("예산은 0 보다 커야 합니다.")
    if "@" not in email:
        raise ValueError("알람을 받을 이메일이 올바르지 않습니다.")

    budget = {
        "BudgetName": BUDGET_NAME,
        "BudgetLimit": {"Amount": f"{limit_usd:.2f}", "Unit": "USD"},
        "TimeUnit": "MONTHLY",
        "BudgetType": "COST",
    }
    notifications = [
        {
            "Notification": {
                "NotificationType": "ACTUAL",
                "ComparisonOperator": "GREATER_THAN",
                "Threshold": threshold,
                "ThresholdType": "PERCENTAGE",
            },
            "Subscribers": [{"SubscriptionType": "EMAIL", "Address": email}],
        }
        for threshold in (80.0, 100.0)
    ]
    return budget, notifications


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--email", required=True, help="알람 받을 이메일")
    ap.add_argument("--limit", type=float, default=15.0, help="월 예산 USD (기본 15)")
    ap.add_argument("--dry-run", action="store_true", help="API 호출 없이 페이로드만 출력")
    args = ap.parse_args()

    budget, notifications = build_payloads(args.email, args.limit)

    if args.dry_run:
        print(json.dumps({"Budget": budget, "Notifications": notifications},
                         ensure_ascii=False, indent=2))
        return 0

    import boto3  # 지연 import — dry-run 은 boto3 없이도 돈다
    sts = boto3.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    client = boto3.client("budgets")

    try:
        client.describe_budget(AccountId=account_id, BudgetName=BUDGET_NAME)
        print(f"이미 있음: {BUDGET_NAME} — 알람만 갱신하려면 콘솔에서 확인하세요.")
        return 0
    except client.exceptions.NotFoundException:
        pass

    client.create_budget(
        AccountId=account_id,
        Budget=budget,
        NotificationsWithSubscribers=notifications,
    )
    print(f"생성됨: {BUDGET_NAME} — 월 ${args.limit:.2f}, 80%/100% 에 {args.email} 로 알람.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
