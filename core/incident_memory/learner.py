"""
IncidentMemory Learner (§35.2).

RemediationRun 성공 시 호출 → IncidentMemoryRecord 자동 생성/갱신.
사용자 동의 (``user_consent``) 가 True 일 때만 영구 저장.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

try:
    from persistence import RecoderDB
    from schemas import IncidentMemoryRecord, RemediationRun
except ImportError:  # pragma: no cover
    from core.persistence import RecoderDB  # type: ignore
    from core.schemas import (  # type: ignore
        IncidentMemoryRecord,
        RemediationRun,
    )

from .memory_store import (
    load_incident_memory,
    save_incident_memory,
)


@dataclass
class LearnResult:
    """학습 호출 결과."""
    stored: bool                       # 실제 DB 에 저장됐는지
    fingerprint: str
    success_count: int                 # 갱신 후 success_count
    skipped_reason: Optional[str] = None


def learn_from_remediation(
    db: RecoderDB,
    *,
    remediation_run: RemediationRun,
    fingerprint: str,
    symptom: str,
    root_cause: str,
    successful_fix: str,
    project_id: Optional[str] = None,
    linked_deployment_id: Optional[str] = None,
    user_consent: bool = False,
) -> LearnResult:
    """단일 RemediationRun 성공으로부터 IncidentMemoryRecord 저장/갱신.

    동작 규칙:
      - ``remediation_run.success`` 가 False 면 학습하지 않음 (실패한 fix 는 기억 안 함)
      - ``user_consent`` 가 False 면 저장하지 않고 메모리 형태로만 반환 (consent 안 받은 학습 금지)
      - 같은 (fingerprint, project_id) 이미 있으면 success_count += 1 + last_seen_at 갱신
      - successful_fix 가 다르면 새 record 가 기존 것을 덮어쓰지 않고 success_count 만 증가 (멱등)

    Args:
        symptom:        사람이 읽을 증상 요약 (마스킹된 값만)
        root_cause:     원인 분석 요약
        successful_fix: 어떻게 고쳤는지 자연어 (마스킹 됨)
    """
    if not remediation_run.success:
        return LearnResult(
            stored=False,
            fingerprint=fingerprint,
            success_count=0,
            skipped_reason="remediation failed - not learned",
        )

    if not user_consent:
        return LearnResult(
            stored=False,
            fingerprint=fingerprint,
            success_count=0,
            skipped_reason="user_consent=False - learning is opt-in",
        )

    existing = load_incident_memory(db, fingerprint, project_id)
    now = datetime.now(timezone.utc)

    if existing is None:
        record = IncidentMemoryRecord(
            fingerprint=fingerprint,
            project_id=project_id,
            symptom=symptom,
            root_cause=root_cause,
            successful_fix=successful_fix,
            applied_proposal_id=remediation_run.proposal_id,
            linked_deployment_id=linked_deployment_id,
            success_count=1,
            last_seen_at=now,
            user_consent=True,
        )
    else:
        # 동일 fingerprint 재발 → success_count 만 증가. 첫 successful_fix 유지.
        record = existing.model_copy(
            update={
                "success_count": existing.success_count + 1,
                "last_seen_at": now,
                "applied_proposal_id": remediation_run.proposal_id,  # 가장 최근 사용된 proposal
                "linked_deployment_id": linked_deployment_id or existing.linked_deployment_id,
            }
        )

    save_incident_memory(db, record)
    return LearnResult(
        stored=True,
        fingerprint=fingerprint,
        success_count=record.success_count,
    )
