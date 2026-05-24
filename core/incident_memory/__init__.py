"""
ReCoder IncidentMemory (§35).

"같은 사고 두 번 반복 방지" — 과거에 성공한 fix 를 결정론적 fingerprint 매칭으로
자동 재제안.

v0 (현재): 완전 일치 fingerprint 매칭만 — LLM 호출 0회, 비용 0원.
v1 (P2):   bge-small ONNX 임베딩 유사도 검색 추가 예정.

학습 흐름:
    1. RemediationRun 성공 (success=True)
    2. ``learner.learn_from_remediation()`` 호출
    3. 사용자가 ``user_consent=True`` 로 설정해뒀으면 IncidentMemoryRecord 저장

매칭 흐름:
    1. 새 incident 발생 (error_type + message + stack)
    2. ``fingerprint.build_incident_fingerprint(...)``
    3. ``matcher.match_incident(...)`` → IncidentMemoryMatch list
    4. 최상위 hit 의 proposal 자동 제안 또는 사용자 검토

Public API
----------
- ``build_incident_fingerprint``
- ``learn_from_remediation``
- ``match_incident``
- ``save_incident_memory / load_incident_memory / list_incident_memories``
- ``init_incident_memory_table``  (RecoderDB 확장)
"""

from __future__ import annotations

from .fingerprint import (
    build_incident_fingerprint,
    mask_for_fingerprint,
    normalize_stack_trace,
)
from .learner import LearnResult, learn_from_remediation
from .matcher import match_incident, rank_matches
from .memory_store import (
    delete_incident_memory,
    init_incident_memory_table,
    list_incident_memories,
    load_incident_memory,
    save_incident_memory,
    touch_incident_memory,
)

__all__ = [
    "build_incident_fingerprint",
    "mask_for_fingerprint",
    "normalize_stack_trace",
    "learn_from_remediation",
    "LearnResult",
    "match_incident",
    "rank_matches",
    "save_incident_memory",
    "load_incident_memory",
    "list_incident_memories",
    "touch_incident_memory",
    "delete_incident_memory",
    "init_incident_memory_table",
]
