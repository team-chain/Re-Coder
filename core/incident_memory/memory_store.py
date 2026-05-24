"""
IncidentMemory SQLite store (§35.1).

기존 ``persistence.RecoderDB`` 의 동일한 SQLite 파일을 재사용. 별도 테이블
``incident_memory`` 를 IF NOT EXISTS 로 생성한다 (기존 3-Layer DDL 과 독립).

사용 흐름:
    db = RecoderDB(path)
    init_incident_memory_table(db)        # 최초 1회 (idempotent)
    save_incident_memory(db, record)
    matches = list_incident_memories(db, fingerprint=fp)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

try:
    from persistence import RecoderDB
    from schemas import IncidentMemoryRecord
except ImportError:  # pragma: no cover
    from core.persistence import RecoderDB  # type: ignore
    from core.schemas import IncidentMemoryRecord  # type: ignore


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------


_INCIDENT_MEMORY_DDL: str = """
CREATE TABLE IF NOT EXISTS incident_memory (
    fingerprint           TEXT NOT NULL,
    project_id            TEXT,
    symptom               TEXT NOT NULL,
    root_cause            TEXT NOT NULL,
    successful_fix        TEXT NOT NULL,
    applied_proposal_id   TEXT NOT NULL,
    linked_deployment_id  TEXT,
    success_count         INTEGER NOT NULL DEFAULT 1,
    last_seen_at          TEXT NOT NULL,
    user_consent          INTEGER NOT NULL DEFAULT 0,
    payload               TEXT NOT NULL,
    PRIMARY KEY (fingerprint, project_id)
);
CREATE INDEX IF NOT EXISTS idx_incident_memory_fingerprint
    ON incident_memory(fingerprint);
CREATE INDEX IF NOT EXISTS idx_incident_memory_project
    ON incident_memory(project_id);
CREATE INDEX IF NOT EXISTS idx_incident_memory_last_seen
    ON incident_memory(last_seen_at DESC);
"""


def init_incident_memory_table(db: RecoderDB) -> None:
    """``incident_memory`` 테이블 생성. idempotent."""
    with db.connect() as conn:
        conn.executescript(_INCIDENT_MEMORY_DDL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _project_key(project_id: Optional[str]) -> str:
    """SQLite PK 일부 — None 을 빈 문자열로 통일 (NULL 은 PK 매칭 어렵게 함)."""
    return project_id or ""


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def save_incident_memory(db: RecoderDB, record: IncidentMemoryRecord) -> str:
    """단일 IncidentMemoryRecord 저장. 같은 (fingerprint, project_id) 가 있으면 REPLACE.

    Returns:
        fingerprint
    """
    payload = record.model_dump_json()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO incident_memory
                (fingerprint, project_id, symptom, root_cause, successful_fix,
                 applied_proposal_id, linked_deployment_id, success_count,
                 last_seen_at, user_consent, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.fingerprint,
                _project_key(record.project_id),
                record.symptom,
                record.root_cause,
                record.successful_fix,
                record.applied_proposal_id,
                record.linked_deployment_id,
                record.success_count,
                _to_iso(record.last_seen_at),
                1 if record.user_consent else 0,
                payload,
            ),
        )
    return record.fingerprint


def load_incident_memory(
    db: RecoderDB,
    fingerprint: str,
    project_id: Optional[str] = None,
) -> Optional[IncidentMemoryRecord]:
    """fingerprint (+ project_id) 로 단일 record 조회."""
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT payload FROM incident_memory
            WHERE fingerprint = ? AND project_id = ?
            """,
            (fingerprint, _project_key(project_id)),
        ).fetchone()
    if row is None:
        return None
    return IncidentMemoryRecord.model_validate_json(row["payload"])


def list_incident_memories(
    db: RecoderDB,
    *,
    fingerprint: Optional[str] = None,
    project_id: Optional[str] = None,
    consent_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> list[IncidentMemoryRecord]:
    """필터 조건에 맞는 IncidentMemoryRecord 조회 (최근 last_seen 순).

    Args:
        consent_only: True 면 user_consent=1 만 반환 (기본). 학습 prompt 에서
                      소비될 데이터는 사용자 동의가 있어야 한다.
    """
    where_clauses: list[str] = []
    params: list[object] = []
    if fingerprint is not None:
        where_clauses.append("fingerprint = ?")
        params.append(fingerprint)
    if project_id is not None:
        where_clauses.append("project_id = ?")
        params.append(_project_key(project_id))
    if consent_only:
        where_clauses.append("user_consent = 1")
    where = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    query = f"SELECT payload FROM incident_memory{where} ORDER BY last_seen_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with db.connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [IncidentMemoryRecord.model_validate_json(r["payload"]) for r in rows]


def touch_incident_memory(
    db: RecoderDB,
    fingerprint: str,
    project_id: Optional[str] = None,
) -> Optional[IncidentMemoryRecord]:
    """재발 시: success_count += 1, last_seen_at = now. 없으면 None.

    이 함수는 매칭 hit 직후 호출돼 "이 fix 가 또 효과 있었다" 를 기록한다.
    """
    existing = load_incident_memory(db, fingerprint, project_id)
    if existing is None:
        return None
    existing.success_count += 1
    existing.last_seen_at = datetime.now(timezone.utc)
    save_incident_memory(db, existing)
    return existing


def delete_incident_memory(
    db: RecoderDB,
    fingerprint: str,
    project_id: Optional[str] = None,
) -> bool:
    """사용자가 명시적으로 학습 데이터 제거 요청 시. GDPR / 옵트아웃 지원."""
    with db.transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM incident_memory WHERE fingerprint = ? AND project_id = ?",
            (fingerprint, _project_key(project_id)),
        )
        return cursor.rowcount > 0
