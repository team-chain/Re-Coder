"""scripts/setup_budget.py 페이로드 고정 테스트.

mvp-acceptance-checklist.md E4 ("✅ budget payload")가 가리키는 계약:
CreateBudget 페이로드의 이름/단위/주기와 80%/100% 이메일 알람 구조가
바뀌면 여기서 먼저 깨진다. dry-run 경로는 boto3 없이 돌아야 한다.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup_budget.py"
_spec = importlib.util.spec_from_file_location("setup_budget", _SCRIPT)
setup_budget = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup_budget)


class TestBuildPayloads:
    def test_budget_shape(self):
        budget, _ = setup_budget.build_payloads("dev@example.com", 15)
        assert budget == {
            "BudgetName": "recoder-monthly",
            "BudgetLimit": {"Amount": "15.00", "Unit": "USD"},
            "TimeUnit": "MONTHLY",
            "BudgetType": "COST",
        }

    def test_amount_formatting_two_decimals(self):
        budget, _ = setup_budget.build_payloads("dev@example.com", 7.5)
        assert budget["BudgetLimit"]["Amount"] == "7.50"

    def test_notifications_80_and_100_email(self):
        _, notifications = setup_budget.build_payloads("dev@example.com", 15)
        assert [n["Notification"]["Threshold"] for n in notifications] == [80.0, 100.0]
        for n in notifications:
            assert n["Notification"]["NotificationType"] == "ACTUAL"
            assert n["Notification"]["ComparisonOperator"] == "GREATER_THAN"
            assert n["Notification"]["ThresholdType"] == "PERCENTAGE"
            assert n["Subscribers"] == [
                {"SubscriptionType": "EMAIL", "Address": "dev@example.com"}
            ]

    def test_rejects_nonpositive_limit(self):
        with pytest.raises(ValueError):
            setup_budget.build_payloads("dev@example.com", 0)
        with pytest.raises(ValueError):
            setup_budget.build_payloads("dev@example.com", -3)

    def test_rejects_bad_email(self):
        with pytest.raises(ValueError):
            setup_budget.build_payloads("not-an-email", 15)

    def test_dry_run_needs_no_boto3(self, monkeypatch, capsys):
        # boto3 import 를 강제로 실패시켜도 dry-run 은 성공해야 한다.
        monkeypatch.setitem(sys.modules, "boto3", None)
        monkeypatch.setattr(
            sys, "argv",
            ["setup_budget.py", "--email", "dev@example.com", "--dry-run"],
        )
        assert setup_budget.main() == 0
        out = capsys.readouterr().out
        assert '"recoder-monthly"' in out
        assert '"80.0"' in out or "80.0" in out
