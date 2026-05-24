"""
ReCoder Continuous Verification (§34).

배포 직후 5분 (또는 contract.duration) 동안 컨테이너를 감시해서 자동 rollback
조건을 평가한다. B-1 Runtime Preflight 가 "배포 시점" 검증이라면, B-2 CV 는
"배포 직후 운영 안정성" 검증.

데이터 수집:
    - health probe 실패 카운트 (interval 폴링)
    - 분당 에러 로그 발생률 (docker logs --since)
    - 메모리 사용률 (docker stats --no-stream)

평가:
    - ContractRollbackStrategy.auto_rollback_on 의 단위 트리거 OR 조건
    - 하나라도 트리거 발동 시 status = AUTO_ROLLBACK_PROPOSED + rollback target 결정

영속화:
    - CVResult 모델로 결과 반환 (호출자가 DeploymentLedger 에 통합)
    - DeploymentLedger.status: DEPLOYING -> STABLE / ROLLED_BACK 전이

Public API
----------
- ``CVMonitor(deployment_id, contract, ...).run() -> CVResult``
- ``evaluate_triggers(triggers, observed) -> AutoRollbackDecision``
- ``select_rollback_target(deployment_id, db) -> Optional[str]``
- ``run_cv_async(...)``
"""

from __future__ import annotations

from .monitor import CVMonitor, CVObservation, run_cv_sync
from .triggers import (
    AutoRollbackDecision,
    evaluate_triggers,
    select_rollback_target,
)

__all__ = [
    "CVMonitor",
    "CVObservation",
    "run_cv_sync",
    "AutoRollbackDecision",
    "evaluate_triggers",
    "select_rollback_target",
]
