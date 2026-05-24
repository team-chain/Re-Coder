"""
ReCoder v10 Backbone Eval (§38, §44).

A-1 ~ A-5 백본 (Static Preflight + RemediationProposal + 3-Layer + IncidentMemory)
의 정확성, 결정성, 안전성을 6개 카테고리로 평가하고 CI Gate 를 통과시킨다.

기존 ``core/eval/harness.py`` (v6.4 PatchProposal 평가) 와 공존. 본 모듈은
v10 backbone 전용.

카테고리:
    1. PREFLIGHT_ACCURACY      : 12 검사가 적절한 blocker/warning 발생
    2. REMEDIATION_DETERMINISM : 같은 입력 → 같은 proposal_id (5회 반복)
    3. REMEDIATION_APPLY       : proposal 적용 후 preflight 재실행 → 상태 호전
    4. INCIDENT_FINGERPRINT    : fingerprint 결정성 + 마스킹 누수 없음
    5. INCIDENT_MATCH          : 매칭 precision/recall
    6. SAFETY_REGRESSIONS      : CRITICAL 패턴 (curl|sh, AWS key 등) 항상 차단

Public API
----------
- ``run_v10_eval(...)``       : 단일 진입점 → EvalV10Report
- ``run_v10_gate(report, *, min_pass_rate=0.95)``: bool — CI 통과 여부
- ``EvalV10Report``           : 결과 모델
"""

from __future__ import annotations

from .categories import CATEGORIES, V10EvalCategory
from .gate import GateResult, run_v10_gate
from .runner import EvalV10Report, EvalV10Result, run_v10_eval

__all__ = [
    "CATEGORIES",
    "V10EvalCategory",
    "EvalV10Report",
    "EvalV10Result",
    "GateResult",
    "run_v10_eval",
    "run_v10_gate",
]
