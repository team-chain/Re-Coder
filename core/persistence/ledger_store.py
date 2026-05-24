"""
Layer 3 — DeploymentLedger CRUD (§33.3).

**Append-only**: ``save_deployment()`` 는 INSERT 만, ``update_deployment_status()`` 만
상태 전이 허용 (DEPLOYING → STABLE/FAILED/ROLLED_BACK). DELETE 는 ``RecoderDB.purge_all()``
(테스트 전용) 외 금지.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

try:
    from schemas import DeploymentLedger, DeploymentLedgerStatus
except ImportError:  # pragma: no cover
    from core.schemas import DeploymentLedger, DeploymentLedgerStatus  # type: ignore

from .db import RecoderDB


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def save_deployment(db: RecoderDB, ledger: DeploymentLedger) -> str:
    """**최초 1회만**. 같은 deployment_id 존재 시 IntegrityError.

    상태 갱신은 ``update_deployment_status()`` 사용.
    """
    payload = ledger.model_dump_json()
    status = ledger.status.value if hasattr(ledger.status, "value") else str(ledger.status)
    with db.transaction() as conn:
        # INSERT (NOT OR REPLACE — append-only 보호)
        conn.execute(
            """
            INSERT INTO deployments
                (deployment_id, project_id, preflight_run_id, contract_hash,
                 git_commit, image_digest, status, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ledger.deployment_id,
                ledger.project_id,
                ledger.preflight_run_id,
                ledger.contract_hash,
                ledger.git_commit,
                ledger.image_digest,
                status,
                payload,
                _to_iso(ledger.created_at),
            ),
        )
    return ledger.deployment_id


def update_deployment_status(
    db: RecoderDB,
    deployment_id: str,
    new_status: DeploymentLedgerStatus,
    *,
    health_after: Optional[str] = None,
    failure_reason: Optional[str] = None,
) -> bool:
    """상태 전이만 허용 (DEPLOYING → STABLE/FAILED/ROLLED_BACK).

    payload 의 status/health_after/failure_reason 도 함께 갱신.

    Returns:
        True 면 갱신 됨. False 면 deployment 없거나 invalid transition.
    """
    valid_transitions: dict[DeploymentLedgerStatus, set[DeploymentLedgerStatus]] = {
        DeploymentLedgerStatus.DEPLOYING: {
            DeploymentLedgerStatus.STABLE,
            DeploymentLedgerStatus.FAILED,
            DeploymentLedgerStatus.ROLLED_BACK,
        },
        DeploymentLedgerStatus.STABLE:      {DeploymentLedgerStatus.ROLLED_BACK},
        DeploymentLedgerStatus.FAILED:      {DeploymentLedgerStatus.ROLLED_BACK},
        DeploymentLedgerStatus.ROLLED_BACK: set(),  # terminal
    }

    existing = load_deployment(db, deployment_id)
    if existing is None:
        return False
    current_status = existing.status
    if isinstance(current_status, str):
        current_status = DeploymentLedgerStatus(current_status)
    if new_status not in valid_transitions[current_status]:
        return False

    # 갱신된 payload (Pydantic 통한 검증)
    existing.status = new_status
    if health_after is not None:
        # Literal["healthy","unhealthy"] 검증을 model_validate 우회 — 직접 캐스팅
        existing.health_after = health_after  # type: ignore[assignment]
    if failure_reason is not None:
        existing.failure_reason = failure_reason
    payload = existing.model_dump_json()

    status_str = new_status.value if hasattr(new_status, "value") else str(new_status)
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE deployments
            SET status = ?, payload = ?
            WHERE deployment_id = ?
            """,
            (status_str, payload, deployment_id),
        )
    return True


def load_deployment(db: RecoderDB, deployment_id: str) -> Optional[DeploymentLedger]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM deployments WHERE deployment_id = ?",
            (deployment_id,),
        ).fetchone()
    if row is None:
        return None
    return DeploymentLedger.model_validate_json(row["payload"])


def list_deployments(
    db: RecoderDB,
    *,
    project_id: Optional[str] = None,
    status: Optional[DeploymentLedgerStatus] = None,
    contract_hash: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[DeploymentLedger]:
    where_clauses: list[str] = []
    params: list[object] = []
    if project_id is not None:
        where_clauses.append("project_id = ?")
        params.append(project_id)
    if status is not None:
        where_clauses.append("status = ?")
        params.append(status.value if hasattr(status, "value") else str(status))
    if contract_hash is not None:
        where_clauses.append("contract_hash = ?")
        params.append(contract_hash)
    where = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    query = f"SELECT payload FROM deployments{where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with db.connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [DeploymentLedger.model_validate_json(r["payload"]) for r in rows]


def list_rollback_candidates(
    db: RecoderDB,
    *,
    project_id: str,
    limit: int = 5,
) -> list[DeploymentLedger]:
    """이전에 STABLE 이었던 배포들 중 최신 N개 (rollback target 후보)."""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT payload FROM deployments
            WHERE project_id = ? AND status = ? AND image_digest IS NOT NULL
            ORDER BY created_at DESC LIMIT ?
            """,
            (project_id, DeploymentLedgerStatus.STABLE.value, limit),
        ).fetchall()
    return [DeploymentLedger.model_validate_json(r["payload"]) for r in rows]
