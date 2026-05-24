"""
Layer 1 — PreflightRun CRUD (§33.1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

try:
    from schemas import PreflightRun, PreflightStatus
except ImportError:  # pragma: no cover
    from core.schemas import PreflightRun, PreflightStatus  # type: ignore

from .db import RecoderDB


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def save_preflight_run(db: RecoderDB, run: PreflightRun) -> str:
    """PreflightRun 영속화. 기존 id 가 있으면 REPLACE.

    Returns:
        preflight_run_id
    """
    payload = run.model_dump_json()
    status = run.status.value if hasattr(run.status, "value") else str(run.status)
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO preflight_runs
                (preflight_run_id, project_id, contract_hash, status, score, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.preflight_run_id,
                run.project_id,
                run.contract_hash,
                status,
                run.score,
                payload,
                _to_iso(run.created_at),
            ),
        )
    return run.preflight_run_id


def load_preflight_run(db: RecoderDB, preflight_run_id: str) -> Optional[PreflightRun]:
    """id 로 PreflightRun 복원. 없으면 None."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM preflight_runs WHERE preflight_run_id = ?",
            (preflight_run_id,),
        ).fetchone()
    if row is None:
        return None
    return PreflightRun.model_validate_json(row["payload"])


def list_preflight_runs(
    db: RecoderDB,
    *,
    project_id: Optional[str] = None,
    status: Optional[PreflightStatus] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PreflightRun]:
    """최근순으로 PreflightRun 조회."""
    where_clauses: list[str] = []
    params: list[object] = []
    if project_id is not None:
        where_clauses.append("project_id = ?")
        params.append(project_id)
    if status is not None:
        where_clauses.append("status = ?")
        params.append(status.value if hasattr(status, "value") else str(status))
    where = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    query = f"SELECT payload FROM preflight_runs{where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with db.connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [PreflightRun.model_validate_json(r["payload"]) for r in rows]


def count_preflight_runs(
    db: RecoderDB,
    *,
    project_id: Optional[str] = None,
    status: Optional[PreflightStatus] = None,
) -> int:
    """필터 조건에 맞는 레코드 개수."""
    where_clauses: list[str] = []
    params: list[object] = []
    if project_id is not None:
        where_clauses.append("project_id = ?")
        params.append(project_id)
    if status is not None:
        where_clauses.append("status = ?")
        params.append(status.value if hasattr(status, "value") else str(status))
    where = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    query = f"SELECT COUNT(*) AS n FROM preflight_runs{where}"
    with db.connect() as conn:
        row = conn.execute(query, tuple(params)).fetchone()
    return int(row["n"])
