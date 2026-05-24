"""
Unit tests for v10 Eval Harness (§38).

harness 자체가 정상 동작하는지 확인 — golden 케이스가 모두 통과되어야 정상.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[2]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from eval.v10 import (  # noqa: E402
    CATEGORIES,
    EvalV10Report,
    GateResult,
    V10EvalCategory,
    run_v10_eval,
    run_v10_gate,
)


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


def test_categories__all_6_defined() -> None:
    assert len(CATEGORIES) == 6
    names = {c.value for c in CATEGORIES}
    assert names == {
        "preflight_accuracy",
        "remediation_determinism",
        "remediation_apply",
        "incident_fingerprint",
        "incident_match",
        "safety_regressions",
    }


def test_eval__produces_results() -> None:
    """전체 평가 실행 + 결과 구조 검증."""
    report = run_v10_eval()
    assert isinstance(report, EvalV10Report)
    assert report.total > 0
    assert report.finished_at_ms >= report.started_at_ms


def test_eval__no_exceptions() -> None:
    """어떤 케이스도 예외로 죽어선 안 됨."""
    report = run_v10_eval()
    exc_cases = [r for r in report.results if r.error is not None]
    assert exc_cases == [], (
        f"Exception 발생: {[(r.case_id, r.error) for r in exc_cases]}"
    )


def test_eval__all_6_categories_have_results() -> None:
    report = run_v10_eval()
    by_cat = report.by_category
    for cat in V10EvalCategory:
        assert cat.value in by_cat, f"카테고리 {cat.value} 의 결과가 없음"
        assert by_cat[cat.value]["total"] >= 1


def test_eval__overall_pass_rate_high() -> None:
    """A-1~A-5 구현이 정상이면 통과율 100% 기대 (적어도 95%)."""
    report = run_v10_eval()
    assert report.pass_rate >= 0.95, (
        f"pass_rate={report.pass_rate:.2%} < 95% — backbone 회귀 의심.\n"
        f"실패 케이스: {[r.case_id for r in report.results if not r.passed]}"
    )


def test_eval__no_safety_violations() -> None:
    """SAFETY_REGRESSIONS 는 단 1건도 실패해선 안 됨."""
    report = run_v10_eval()
    assert report.safety_violations == 0, (
        f"Safety 회귀 {report.safety_violations}건"
    )


def test_eval__weighted_pass_rate_high() -> None:
    report = run_v10_eval()
    assert report.weighted_pass_rate >= 0.95


# ---------------------------------------------------------------------------
# Gate behavior
# ---------------------------------------------------------------------------


def test_gate__passes_on_clean_report() -> None:
    report = run_v10_eval()
    gate = run_v10_gate(report)
    assert gate.passed, f"Gate failed: {gate.reasons}"
    assert gate.weighted_pass_rate >= 0.95
    assert gate.safety_violations == 0


def test_gate__fails_when_safety_violation() -> None:
    """SAFETY_REGRESSIONS 카테고리에 실패를 주입하면 게이트 차단."""
    report = run_v10_eval()
    # 임의로 safety case 한 개를 실패로 변경
    for r in report.results:
        if r.category == V10EvalCategory.SAFETY_REGRESSIONS:
            r.passed = False
            break
    gate = run_v10_gate(report)
    assert gate.passed is False
    assert gate.safety_violations >= 1


def test_gate__fails_when_threshold_below() -> None:
    """min_pass_rate 를 1.01 같이 비정상 임계값으로 두면 무조건 실패."""
    report = run_v10_eval()
    gate = run_v10_gate(report, min_pass_rate=1.01)
    assert gate.passed is False
    assert any("weighted_pass_rate" in r for r in gate.reasons)


def test_gate__fails_when_case_raises_exception() -> None:
    """case 에서 예외가 발생하면 (error 필드 set) 기본 모드에서 게이트 차단."""
    report = run_v10_eval()
    # 임의로 한 결과에 error 주입
    report.results[0].error = "synthetic error for test"
    gate = run_v10_gate(report)
    assert gate.passed is False
    assert any("exception" in r for r in gate.reasons)


def test_gate__allow_exceptions_overrides() -> None:
    report = run_v10_eval()
    report.results[0].error = "synthetic"
    gate = run_v10_gate(report, allow_exceptions=True)
    # error 만 빼면 통과 가능 — 다른 조건이 다 ok 면 pass
    if report.safety_violations == 0 and report.weighted_pass_rate >= 0.95:
        assert gate.passed is True


# ---------------------------------------------------------------------------
# Determinism category — 12 codes
# ---------------------------------------------------------------------------


def test_determinism_category__covers_all_12_codes() -> None:
    """REMEDIATION_DETERMINISM 카테고리는 12 종 PreflightCheckCode 모두 평가."""
    report = run_v10_eval()
    det_cases = [
        r for r in report.results
        if r.category == V10EvalCategory.REMEDIATION_DETERMINISM
    ]
    assert len(det_cases) == 12


# ---------------------------------------------------------------------------
# CLI entry — smoke
# ---------------------------------------------------------------------------


def test_cli__import_main_module() -> None:
    """`python -m eval.v10` 이 import 단계에서 실패하지 않는지."""
    import importlib
    mod = importlib.import_module("eval.v10.__main__")
    assert hasattr(mod, "main")
