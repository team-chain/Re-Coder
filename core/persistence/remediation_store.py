"""
Layer 2 — RemediationRun CRUD (§33.2).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

try:
    from schemas import RemediationRun
except ImportError:  # pragma: no cover
    from core.schemas import RemediationRun  # type: ignore

from .db import RecoderDB


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def save_remediation_run(db: RecoderDB, run: RemediationRun) -> str:
    """RemediationRun 영속화. 같은 id 가 있으면 REPLACE.

    FK 제약: preflight_run_id 가 존재해야 함. 없으면 IntegrityError.
    """
    payload = run.model_dump_json()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO remediation_runs
                (remediation_run_id, preflight_run_id, proposal_id,
                 success, rollback_executed, payload, applied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.remediation_run_id,
                run.preflight_run_id,
                run.proposal_id,
                1 if run.success else 0,
                1 if run.rollback_executed else 0,
                payload,
                _to_iso(run.applied_at),
            ),
        )
    return run.remediation_run_id


def load_remediation_run(db: RecoderDB, remediation_run_id: str) -> Optional[RemediationRun]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM remediation_runs WHERE remediation_run_id = ?",
            (remediation_run_id,),
        ).fetchone()
    if row is None:
        return None
    return RemediationRun.model_validate_json(row["payload"])


def list_remediation_runs(
    db: RecoderDB,
    *,
    preflight_run_id: Optional[str] = None,
    proposal_id: Optional[str] = None,
    success_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[RemediationRun]:
    """최근순 RemediationRun 조회. 다양한 필터 지원."""
    where_clauses: list[str] = []
    params: list[object] = []
    if preflight_run_id is not None:
        where_clauses.append("preflight_run_id = ?")
        params.append(preflight_run_id)
    if proposal_id is not None:
        where_clauses.append("proposal_id = ?")
        params.append(proposal_id)
    if success_only:
        where_clauses.append("success = 1")
    where = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    query = f"SELECT payload FROM remediation_runs{where} ORDER BY applied_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with db.connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [RemediationRun.model_validate_json(r["payload"]) for r in rows]


def count_remediation_runs_by_proposal(db: RecoderDB, proposal_id: str) -> dict[str, int]:
    """해당 proposal 이 몇 번 적용됐고 몇 번 성공/실패/롤백됐는지."""
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                    AS total,
                SUM(CASE WHEN success=1            THEN 1 ELSE 0 END) AS success,
                SUM(CASE WHEN success=0            THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN rollback_executed=1  THEN 1 ELSE 0 END) AS rolled_back
            FROM remediation_runs WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()
    return {
        "total":       int(row["total"] or 0),
        "success":     int(row["success"] or 0),
        "failed":      int(row["failed"] or 0),
        "rolled_back": int(row["rolled_back"] or 0),
    }
