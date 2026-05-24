"""
CV 자동 rollback 트리거 평가 — 순수 함수 (§34).

ContractRollbackStrategy.auto_rollback_on 의 단위 트리거를 OR 로 결합.
관찰값 (CVObservation) 이 하나라도 트리거를 만족하면 rollback 제안.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

try:
    from persistence import RecoderDB, list_rollback_candidates
    from schemas import (
        ContractAutoRollbackTrigger,
        ContractRollbackStrategy,
        DeploymentLedger,
        DeploymentLedgerStatus,
    )
except ImportError:  # pragma: no cover
    from core.persistence import RecoderDB, list_rollback_candidates  # type: ignore
    from core.schemas import (  # type: ignore
        ContractAutoRollbackTrigger,
        ContractRollbackStrategy,
        DeploymentLedger,
        DeploymentLedgerStatus,
    )

try:
    from persistence.ledger_store import list_rollback_candidates as _list_rb  # noqa: F401
except ImportError:  # pragma: no cover
    pass


@dataclass
class AutoRollbackDecision:
    """단위 트리거 평가 결과."""
    should_rollback:    bool = False
    triggered_by:       list[str] = field(default_factory=list)  # 발동된 트리거 이름들
    notes:              list[str] = field(default_factory=list)


def evaluate_triggers(
    strategy: ContractRollbackStrategy,
    *,
    health_failure_count: int,
    error_log_rate: float,
    max_memory_pct: float,
) -> AutoRollbackDecision:
    """관찰값을 ContractRollbackStrategy.auto_rollback_on 트리거 리스트와 비교.

    Args:
        strategy:              contract.operational_policy.rollback_strategy
        health_failure_count:  CV 기간 내 누적 health probe 실패 횟수
        error_log_rate:        분당 평균 에러 로그 발생률
        max_memory_pct:        CV 기간 내 메모리 사용률 최대값 (0~1)

    Returns:
        AutoRollbackDecision — should_rollback=True 면 자동 rollback 제안.

    트리거 결합: 여러 ContractAutoRollbackTrigger 가 정의되면 OR.
    단일 트리거 내부의 필드 여러 개는 AND (모두 충족 시 발동).
    """
    decision = AutoRollbackDecision()

    if strategy.type == "manual":
        decision.notes.append("rollback_strategy.type=manual — 자동 rollback 비활성")
        return decision

    for idx, trigger in enumerate(strategy.auto_rollback_on or []):
        if _trigger_fires(trigger, health_failure_count, error_log_rate, max_memory_pct):
            name = _trigger_label(idx, trigger)
            decision.should_rollback = True
            decision.triggered_by.append(name)

    return decision


def _trigger_fires(
    trigger: ContractAutoRollbackTrigger,
    health_failure_count: int,
    error_log_rate: float,
    max_memory_pct: float,
) -> bool:
    """단일 ContractAutoRollbackTrigger 평가 — 내부 필드는 모두 AND."""
    conditions: list[bool] = []
    if trigger.health_check_fail_count is not None:
        conditions.append(health_failure_count >= trigger.health_check_fail_count)
    if trigger.error_log_rate_exceeded is True:
        # 임계 자체는 contract.continuous_verification.error_log_threshold 에서.
        # 호출자가 error_log_rate > threshold 이미 계산해서 넘겨주거나,
        # 여기서는 단순히 error_log_rate > 0 만 봄 (보수적).
        conditions.append(error_log_rate > 0)
    if trigger.memory_usage_exceeded is not None:
        conditions.append(max_memory_pct >= trigger.memory_usage_exceeded)
    # 빈 트리거는 발동 안 함
    return bool(conditions) and all(conditions)


def _trigger_label(idx: int, trigger: ContractAutoRollbackTrigger) -> str:
    """가독성 좋은 트리거 라벨."""
    parts: list[str] = []
    if trigger.health_check_fail_count is not None:
        parts.append(f"health_fail>={trigger.health_check_fail_count}")
    if trigger.error_log_rate_exceeded is True:
        parts.append("error_log_rate_exceeded")
    if trigger.memory_usage_exceeded is not None:
        parts.append(f"memory>={trigger.memory_usage_exceeded:.0%}")
    if not parts:
        parts.append("empty_trigger")
    return f"#{idx}[" + ",".join(parts) + "]"


# ---------------------------------------------------------------------------
# Rollback target 선택
# ---------------------------------------------------------------------------


def select_rollback_target(
    db: RecoderDB,
    *,
    project_id: str,
    exclude_deployment_id: Optional[str] = None,
) -> Optional[DeploymentLedger]:
    """이전에 STABLE 이었던 가장 최근 배포를 반환 (rollback target).

    Args:
        exclude_deployment_id: 현재 문제 중인 배포를 제외하기 위함.

    Returns:
        rollback 대상 DeploymentLedger or None.
    """
    candidates = list_rollback_candidates(db, project_id=project_id, limit=10)
    for c in candidates:
        if exclude_deployment_id and c.deployment_id == exclude_deployment_id:
            continue
        if c.image_digest is None:
            continue
        return c
    return None
