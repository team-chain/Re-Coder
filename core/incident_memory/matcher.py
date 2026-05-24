"""
IncidentMemory Matcher (§35.2).

새 incident 발생 시 fingerprint 매칭으로 과거 성공 fix 검색.

v0 알고리즘:
    1. 정확 fingerprint 매칭 (confidence 1.0)
    2. project_id 필터 — 같은 프로젝트 우선
    3. 결과는 success_count desc + last_seen_at desc 로 정렬

v1 확장 여지 (P2):
    - bge-small ONNX 임베딩 → cosine similarity ≥ 0.85 partial match
"""

from __future__ import annotations

from typing import Optional

try:
    from persistence import RecoderDB
    from schemas import IncidentMemoryMatch, IncidentMemoryRecord
except ImportError:  # pragma: no cover
    from core.persistence import RecoderDB  # type: ignore
    from core.schemas import (  # type: ignore
        IncidentMemoryMatch,
        IncidentMemoryRecord,
    )

from .memory_store import list_incident_memories, load_incident_memory


# ---------------------------------------------------------------------------
# Match
# ---------------------------------------------------------------------------


def match_incident(
    db: RecoderDB,
    *,
    fingerprint: str,
    project_id: Optional[str] = None,
    cross_project_fallback: bool = True,
    limit: int = 5,
) -> list[IncidentMemoryMatch]:
    """fingerprint 로 과거 incident 매칭.

    Strategy:
      1. (fingerprint, project_id) exact match → confidence=1.0
      2. project_id 매치 없고 ``cross_project_fallback=True`` 면
         fingerprint 만 매칭 → confidence=0.7 (다른 프로젝트 fix, 참고용)

    Returns:
        IncidentMemoryMatch 리스트 (success_count desc + last_seen desc).
    """
    matches: list[IncidentMemoryMatch] = []

    # 1) project 내 정확 매치
    if project_id is not None:
        rec = load_incident_memory(db, fingerprint, project_id)
        if rec is not None:
            matches.append(IncidentMemoryMatch(entry=rec, confidence=1.0))

    # 2) cross-project fallback
    if not matches and cross_project_fallback:
        # project_id 필터 없이 fingerprint 만 매칭
        candidates = list_incident_memories(
            db,
            fingerprint=fingerprint,
            consent_only=True,
            limit=limit,
        )
        for c in candidates:
            # 본인 프로젝트는 위에서 처리했으니 skip
            if project_id is not None and c.project_id == project_id:
                continue
            matches.append(IncidentMemoryMatch(entry=c, confidence=0.7))

    return rank_matches(matches)[:limit]


def rank_matches(matches: list[IncidentMemoryMatch]) -> list[IncidentMemoryMatch]:
    """매칭 결과 ranking — confidence desc + success_count desc + last_seen desc."""
    return sorted(
        matches,
        key=lambda m: (
            -m.confidence,
            -m.entry.success_count,
            -(m.entry.last_seen_at.timestamp() if m.entry.last_seen_at else 0),
        ),
    )
