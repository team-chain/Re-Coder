"""
Unit tests for Continuous Verification (§34).

검증 영역:
  1. parse_error_log_threshold — "10/min" / "30/sec" / "1/hour"
  2. _parse_mem_percent, _looks_like_error_line — 헬퍼
  3. evaluate_triggers — 4 시나리오 (health/error/memory/manual)
  4. CVMonitor.run() — 짧은 duration + hook 으로 metrics 주입
  5. select_rollback_target — 이전 STABLE 배포 선택
  6. derive_status — 5 시나리오
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

_CORE = Path(__file__).resolve().parents[2]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from cv import (  # noqa: E402
    AutoRollbackDecision,
    CVMonitor,
    CVObservation,
    evaluate_triggers,
    run_cv_sync,
    select_rollback_target,
)
from cv.monitor import (  # noqa: E402
    _looks_like_error_line,
    _parse_mem_percent,
    parse_error_log_threshold,
)
from persistence import (  # noqa: E402
    RecoderDB,
    save_deployment,
    update_deployment_status,
)
from schemas import (  # noqa: E402
    ContractAutoRollbackTrigger,
    ContractContinuousVerification,
    ContractOperationalPolicy,
    ContractProjectMeta,
    ContractRollbackStrategy,
    ContractRuntime,
    ContractStack,
    CVResultStatus,
    DeploymentLedger,
    DeploymentLedgerStatus,
    ReleaseContract,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_contract(
    *,
    duration: str = "1s",   # 빠른 테스트를 위해 1초
    interval: str = "100ms",  # 100ms tick
    triggers: list[ContractAutoRollbackTrigger] | None = None,
    rollback_type: str = "previous_image",
) -> ReleaseContract:
    cv = ContractContinuousVerification(
        duration=duration,
        health_check_interval=interval,
        error_log_threshold="10/min",
    )
    rs = ContractRollbackStrategy(
        type=rollback_type,
        auto_rollback_on=triggers or [],
    )
    op = ContractOperationalPolicy(continuous_verification=cv, rollback_strategy=rs)
    return ReleaseContract(
        project=ContractProjectMeta(name="test", stack=ContractStack.PYTHON_FASTAPI),
        runtime=ContractRuntime(host_port=18080, app_port=18000),
        operational_policy=op,
    )


# ---------------------------------------------------------------------------
# 1. parse_error_log_threshold
# ---------------------------------------------------------------------------


def test_parse_error_threshold__min() -> None:
    assert parse_error_log_threshold("10/min") == 10.0
    assert parse_error_log_threshold("0.5/minute") == 0.5


def test_parse_error_threshold__sec_to_min() -> None:
    assert parse_error_log_threshold("1/sec") == 60.0
    assert parse_error_log_threshold("0.5/second") == 30.0


def test_parse_error_threshold__hour_to_min() -> None:
    assert parse_error_log_threshold("60/hour") == 1.0


def test_parse_error_threshold__default_on_bad() -> None:
    assert parse_error_log_threshold("") == 10.0
    assert parse_error_log_threshold(None) == 10.0
    assert parse_error_log_threshold("nonsense") == 10.0


# ---------------------------------------------------------------------------
# 2. helpers
# ---------------------------------------------------------------------------


def test_parse_mem_percent__valid() -> None:
    assert abs(_parse_mem_percent("23.45%") - 0.2345) < 1e-9
    assert abs(_parse_mem_percent("100%") - 1.0) < 1e-9
    assert _parse_mem_percent("0%") == 0.0


def test_parse_mem_percent__multiline_takes_first() -> None:
    assert abs(_parse_mem_percent("12.3%\n45.6%") - 0.123) < 1e-9


def test_parse_mem_percent__bad_input() -> None:
    assert _parse_mem_percent("") == 0.0
    assert _parse_mem_percent("xx%") == 0.0


def test_looks_like_error_line__detected() -> None:
    assert _looks_like_error_line("ERROR: something")
    assert _looks_like_error_line("FATAL: db unreachable")
    assert _looks_like_error_line("Traceback (most recent call last):")
    assert _looks_like_error_line("PANIC: out of memory")
    assert _looks_like_error_line("uncaught Exception in handler")


def test_looks_like_error_line__non_error() -> None:
    assert not _looks_like_error_line("INFO: started")
    assert not _looks_like_error_line("")
    assert not _looks_like_error_line("everything is fine")


# ---------------------------------------------------------------------------
# 3. evaluate_triggers
# ---------------------------------------------------------------------------


def test_evaluate__no_triggers_no_rollback() -> None:
    contract = _mk_contract(triggers=[])
    d = evaluate_triggers(
        contract.operational_policy.rollback_strategy,
        health_failure_count=99, error_log_rate=999, max_memory_pct=0.99,
    )
    assert d.should_rollback is False
    assert d.triggered_by == []


def test_evaluate__health_threshold_triggers() -> None:
    contract = _mk_contract(triggers=[
        ContractAutoRollbackTrigger(health_check_fail_count=3),
    ])
    d = evaluate_triggers(
        contract.operational_policy.rollback_strategy,
        health_failure_count=3, error_log_rate=0, max_memory_pct=0.5,
    )
    assert d.should_rollback is True
    assert any("health_fail>=3" in t for t in d.triggered_by)


def test_evaluate__health_below_threshold_no_trigger() -> None:
    contract = _mk_contract(triggers=[
        ContractAutoRollbackTrigger(health_check_fail_count=5),
    ])
    d = evaluate_triggers(
        contract.operational_policy.rollback_strategy,
        health_failure_count=2, error_log_rate=0, max_memory_pct=0.5,
    )
    assert d.should_rollback is False


def test_evaluate__memory_threshold_triggers() -> None:
    contract = _mk_contract(triggers=[
        ContractAutoRollbackTrigger(memory_usage_exceeded=0.90),
    ])
    d = evaluate_triggers(
        contract.operational_policy.rollback_strategy,
        health_failure_count=0, error_log_rate=0, max_memory_pct=0.95,
    )
    assert d.should_rollback is True
    assert any("memory>=" in t for t in d.triggered_by)


def test_evaluate__error_rate_flag_triggers() -> None:
    contract = _mk_contract(triggers=[
        ContractAutoRollbackTrigger(error_log_rate_exceeded=True),
    ])
    d = evaluate_triggers(
        contract.operational_policy.rollback_strategy,
        health_failure_count=0, error_log_rate=15, max_memory_pct=0.3,
    )
    assert d.should_rollback is True


def test_evaluate__multiple_triggers_OR() -> None:
    """여러 트리거는 OR — 하나만 발동돼도 rollback."""
    contract = _mk_contract(triggers=[
        ContractAutoRollbackTrigger(health_check_fail_count=10),   # 발동 X
        ContractAutoRollbackTrigger(memory_usage_exceeded=0.5),    # 발동 O
    ])
    d = evaluate_triggers(
        contract.operational_policy.rollback_strategy,
        health_failure_count=2, error_log_rate=0, max_memory_pct=0.6,
    )
    assert d.should_rollback is True


def test_evaluate__manual_strategy_disables_auto() -> None:
    contract = _mk_contract(
        triggers=[ContractAutoRollbackTrigger(health_check_fail_count=1)],
        rollback_type="manual",
    )
    d = evaluate_triggers(
        contract.operational_policy.rollback_strategy,
        health_failure_count=99, error_log_rate=999, max_memory_pct=0.99,
    )
    assert d.should_rollback is False
    assert any("manual" in n for n in d.notes)


def test_evaluate__intra_trigger_AND() -> None:
    """단일 트리거 내 여러 조건은 AND — 모두 충족해야 발동."""
    contract = _mk_contract(triggers=[
        ContractAutoRollbackTrigger(
            health_check_fail_count=3,
            memory_usage_exceeded=0.9,
        ),
    ])
    # 둘 중 하나만 충족 — AND 못 만족 → 발동 X
    d = evaluate_triggers(
        contract.operational_policy.rollback_strategy,
        health_failure_count=5, error_log_rate=0, max_memory_pct=0.5,
    )
    assert d.should_rollback is False
    # 둘 다 충족 → 발동 O
    d2 = evaluate_triggers(
        contract.operational_policy.rollback_strategy,
        health_failure_count=5, error_log_rate=0, max_memory_pct=0.95,
    )
    assert d2.should_rollback is True


# ---------------------------------------------------------------------------
# 4. CVMonitor.run() — short duration + hook-injected metrics
# ---------------------------------------------------------------------------


def _all_ok_provider(_dt: float) -> CVObservation:
    return CVObservation(health_ok=True, error_log_count=0, memory_pct=0.3)


def _all_fail_provider(_dt: float) -> CVObservation:
    return CVObservation(health_ok=False, error_log_count=5, memory_pct=0.95)


def test_monitor__stable_when_all_ok() -> None:
    contract = _mk_contract(duration="500ms", interval="100ms")
    result = run_cv_sync("dep_test", contract, metrics_provider=_all_ok_provider)
    assert result.status == CVResultStatus.STABLE
    assert result.health_failure_count == 0
    assert result.max_memory_pct == 0.3
    assert result.error_log_rate == 0.0


def test_monitor__warning_on_health_failures_no_trigger() -> None:
    contract = _mk_contract(duration="500ms", interval="100ms", triggers=[])
    result = run_cv_sync("dep_test", contract, metrics_provider=_all_fail_provider)
    # 트리거 없으니 rollback 안 되고, 단순 WARNING
    assert result.status == CVResultStatus.WARNING
    assert result.health_failure_count >= 1
    assert result.max_memory_pct >= 0.9


def test_monitor__auto_rollback_when_trigger_fires() -> None:
    contract = _mk_contract(
        duration="500ms", interval="100ms",
        triggers=[ContractAutoRollbackTrigger(health_check_fail_count=1)],
    )
    result = run_cv_sync("dep_test", contract, metrics_provider=_all_fail_provider)
    assert result.status == CVResultStatus.AUTO_ROLLBACK_PROPOSED
    assert any("triggered:" in n for n in result.notes)


def test_monitor__request_stop_early_exits() -> None:
    """중간에 request_stop 호출하면 즉시 break."""
    contract = _mk_contract(duration="5s", interval="100ms")
    monitor = CVMonitor("dep_x", contract, metrics_provider=_all_ok_provider)
    import threading
    timer = threading.Timer(0.3, monitor.request_stop)
    timer.start()
    result = monitor.run()
    timer.cancel()
    assert result.duration_seconds < 2  # 5s 안 넘김
    assert monitor.stopped


def test_monitor__max_duration_caps_run() -> None:
    """max_duration_seconds 가 contract.duration 보다 짧으면 그쪽 우선."""
    contract = _mk_contract(duration="10s", interval="100ms")
    import time as _t
    t0 = _t.monotonic()
    result = run_cv_sync(
        "dep_x", contract,
        metrics_provider=_all_ok_provider,
        max_duration_seconds=1,
    )
    elapsed = _t.monotonic() - t0
    assert elapsed < 3
    assert result.duration_seconds <= 1


def test_monitor__memory_warning_threshold() -> None:
    """max_memory_pct >= 0.80 이면 WARNING."""
    def high_mem(_dt: float) -> CVObservation:
        return CVObservation(health_ok=True, error_log_count=0, memory_pct=0.85)
    contract = _mk_contract(duration="200ms", interval="50ms", triggers=[])
    result = run_cv_sync("dep_x", contract, metrics_provider=high_mem)
    assert result.status == CVResultStatus.WARNING


# ---------------------------------------------------------------------------
# 5. select_rollback_target
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> RecoderDB:
    return RecoderDB(tmp_path / "test.db", check_same_thread=True)


def test_select_rollback__no_history_returns_none(db: RecoderDB) -> None:
    assert select_rollback_target(db, project_id="P") is None


def test_select_rollback__returns_most_recent_stable(db: RecoderDB) -> None:
    # 3개 STABLE 배포 + 1개 FAILED
    stable_ids: list[str] = []
    for _ in range(3):
        d = DeploymentLedger(
            project_id="P",
            image_digest="sha256:" + ("a" * 8),
        )
        save_deployment(db, d)
        update_deployment_status(db, d.deployment_id, DeploymentLedgerStatus.STABLE)
        stable_ids.append(d.deployment_id)

    # 가장 최근 STABLE 가 선택돼야 함 (DESC ordering)
    target = select_rollback_target(db, project_id="P")
    assert target is not None
    assert target.deployment_id == stable_ids[-1]
    assert target.image_digest is not None


def test_select_rollback__excludes_current_deployment(db: RecoderDB) -> None:
    """현재 문제 발생 중인 배포는 제외."""
    d1 = DeploymentLedger(project_id="P", image_digest="sha256:aaa")
    save_deployment(db, d1)
    update_deployment_status(db, d1.deployment_id, DeploymentLedgerStatus.STABLE)
    d2 = DeploymentLedger(project_id="P", image_digest="sha256:bbb")
    save_deployment(db, d2)
    update_deployment_status(db, d2.deployment_id, DeploymentLedgerStatus.STABLE)

    target = select_rollback_target(db, project_id="P", exclude_deployment_id=d2.deployment_id)
    assert target is not None
    assert target.deployment_id == d1.deployment_id


def test_select_rollback__skips_deployments_without_image_digest(db: RecoderDB) -> None:
    d1 = DeploymentLedger(project_id="P", image_digest=None)
    save_deployment(db, d1)
    update_deployment_status(db, d1.deployment_id, DeploymentLedgerStatus.STABLE)
    # 이건 image_digest=None 이라 list_rollback_candidates 에 안 잡힘
    target = select_rollback_target(db, project_id="P")
    assert target is None


# ---------------------------------------------------------------------------
# 6. End-to-end: trigger + rollback target 선택
# ---------------------------------------------------------------------------


def test_e2e__auto_rollback_then_select_target(db: RecoderDB) -> None:
    # 1. 이전 STABLE 배포 1개 (rollback target 후보)
    prev = DeploymentLedger(project_id="P", image_digest="sha256:prev")
    save_deployment(db, prev)
    update_deployment_status(db, prev.deployment_id, DeploymentLedgerStatus.STABLE)

    # 2. 새 배포 진행 중 (현재 감시 대상)
    current = DeploymentLedger(project_id="P", image_digest="sha256:current")
    save_deployment(db, current)

    # 3. CV 가 트리거 발동
    contract = _mk_contract(
        duration="300ms", interval="100ms",
        triggers=[ContractAutoRollbackTrigger(memory_usage_exceeded=0.5)],
    )
    result = run_cv_sync(current.deployment_id, contract, metrics_provider=_all_fail_provider)
    assert result.status == CVResultStatus.AUTO_ROLLBACK_PROPOSED

    # 4. rollback target 선택 — current 는 제외
    target = select_rollback_target(
        db, project_id="P", exclude_deployment_id=current.deployment_id,
    )
    assert target is not None
    assert target.deployment_id == prev.deployment_id
    assert target.image_digest == "sha256:prev"
