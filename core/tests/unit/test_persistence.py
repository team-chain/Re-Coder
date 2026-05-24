"""
Unit tests for ReCoder 3-Layer persistence (§33).

검증 영역:
  1. DB 초기화 — WAL 모드, foreign_keys ON, DDL 적용
  2. Layer 1 PreflightRun — save/load/list + 필터 + REPLACE
  3. Layer 2 RemediationRun — FK 제약 + 통계 집계
  4. Layer 3 DeploymentLedger — append-only + 상태 전이 머신
  5. CASCADE 삭제 (purge_all)
  6. 동시성 안전 (WAL)
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[2]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from persistence import (  # noqa: E402
    RecoderDB,
    get_default_db_path,
    list_deployments,
    list_preflight_runs,
    list_remediation_runs,
    load_deployment,
    load_preflight_run,
    load_remediation_run,
    save_deployment,
    save_preflight_run,
    save_remediation_run,
    update_deployment_status,
)
from persistence.preflight_store import count_preflight_runs  # noqa: E402
from persistence.remediation_store import count_remediation_runs_by_proposal  # noqa: E402
from persistence.ledger_store import list_rollback_candidates  # noqa: E402
from schemas import (  # noqa: E402
    DeploymentLedger,
    DeploymentLedgerStatus,
    PreflightBlocker,
    PreflightCheckCode,
    PreflightRun,
    PreflightSeverity,
    PreflightStatus,
    PreflightWarning,
    RemediationRun,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> RecoderDB:
    """Each test gets a fresh DB."""
    return RecoderDB(tmp_path / "test.db", check_same_thread=True)


def _mk_preflight(
    status: PreflightStatus = PreflightStatus.PASSED,
    score: int = 100,
    project_id: str | None = "proj_demo",
    contract_hash: str | None = "ch_aaa",
) -> PreflightRun:
    return PreflightRun(
        project_id=project_id,
        contract_hash=contract_hash,
        status=status,
        score=score,
    )


def _mk_remediation(preflight_run_id: str, proposal_id: str = "rem_xxxx") -> RemediationRun:
    return RemediationRun(
        preflight_run_id=preflight_run_id,
        proposal_id=proposal_id,
        success=True,
    )


def _mk_deployment(
    project_id: str | None = "proj_demo",
    preflight_run_id: str | None = None,
    status: DeploymentLedgerStatus = DeploymentLedgerStatus.DEPLOYING,
) -> DeploymentLedger:
    return DeploymentLedger(
        project_id=project_id,
        preflight_run_id=preflight_run_id,
        contract_hash="ch_aaa",
        git_commit="abc1234",
        image_digest="sha256:abc",
        status=status,
    )


# ---------------------------------------------------------------------------
# 1. DB initialization
# ---------------------------------------------------------------------------


def test_db__creates_file_and_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "subdir" / "recoder.db"
    db = RecoderDB(db_path, check_same_thread=True)
    assert db_path.exists()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert {"preflight_runs", "remediation_runs", "deployments"}.issubset(names)


def test_db__pragmas_applied(db: RecoderDB) -> None:
    with db.connect() as conn:
        jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert jm.lower() == "wal"
    assert int(fk) == 1


def test_get_default_db_path__env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "custom.db"
    monkeypatch.setenv("RECODER_DB_PATH", str(target))
    assert get_default_db_path() == target.resolve()


def test_get_default_db_path__workspace_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RECODER_DB_PATH", raising=False)
    path = get_default_db_path(workspace=tmp_path)
    assert path == (tmp_path / ".recoder" / "recoder.db").resolve()


# ---------------------------------------------------------------------------
# 2. Layer 1 — PreflightRun
# ---------------------------------------------------------------------------


def test_preflight__save_and_load(db: RecoderDB) -> None:
    run = _mk_preflight(score=92, status=PreflightStatus.WARN)
    rid = save_preflight_run(db, run)
    loaded = load_preflight_run(db, rid)
    assert loaded is not None
    assert loaded.preflight_run_id == rid
    assert loaded.score == 92
    assert loaded.status == PreflightStatus.WARN


def test_preflight__load_missing_returns_none(db: RecoderDB) -> None:
    assert load_preflight_run(db, "nonexistent") is None


def test_preflight__list_orders_recent_first(db: RecoderDB) -> None:
    ids = []
    for i in range(3):
        r = _mk_preflight(score=70 + i)
        ids.append(save_preflight_run(db, r))
    items = list_preflight_runs(db)
    assert len(items) == 3
    # 가장 최근에 저장된 것이 맨 위
    assert items[0].preflight_run_id == ids[-1]


def test_preflight__filter_by_status(db: RecoderDB) -> None:
    save_preflight_run(db, _mk_preflight(status=PreflightStatus.PASSED))
    save_preflight_run(db, _mk_preflight(status=PreflightStatus.BLOCKED))
    save_preflight_run(db, _mk_preflight(status=PreflightStatus.BLOCKED))
    blocked = list_preflight_runs(db, status=PreflightStatus.BLOCKED)
    assert len(blocked) == 2
    assert all(r.status == PreflightStatus.BLOCKED for r in blocked)


def test_preflight__filter_by_project(db: RecoderDB) -> None:
    save_preflight_run(db, _mk_preflight(project_id="A"))
    save_preflight_run(db, _mk_preflight(project_id="B"))
    a = list_preflight_runs(db, project_id="A")
    assert len(a) == 1
    assert a[0].project_id == "A"


def test_preflight__count(db: RecoderDB) -> None:
    save_preflight_run(db, _mk_preflight(status=PreflightStatus.PASSED))
    save_preflight_run(db, _mk_preflight(status=PreflightStatus.WARN))
    save_preflight_run(db, _mk_preflight(status=PreflightStatus.BLOCKED))
    assert count_preflight_runs(db) == 3
    assert count_preflight_runs(db, status=PreflightStatus.BLOCKED) == 1


def test_preflight__replace_same_id(db: RecoderDB) -> None:
    run = _mk_preflight(score=50)
    save_preflight_run(db, run)
    run.score = 75
    save_preflight_run(db, run)
    loaded = load_preflight_run(db, run.preflight_run_id)
    assert loaded.score == 75
    assert count_preflight_runs(db) == 1  # replaced, not inserted


def test_preflight__roundtrip_preserves_blockers_warnings(db: RecoderDB) -> None:
    run = PreflightRun(
        project_id="p",
        status=PreflightStatus.BLOCKED,
        score=40,
        blockers=[PreflightBlocker(
            code=PreflightCheckCode.MISSING_DOCKERFILE,
            message="msg",
            severity=PreflightSeverity.HIGH,
        )],
        warnings=[PreflightWarning(
            code=PreflightCheckCode.UNPINNED_DEPENDENCIES,
            message="warn",
            severity=PreflightSeverity.LOW,
        )],
    )
    rid = save_preflight_run(db, run)
    loaded = load_preflight_run(db, rid)
    assert len(loaded.blockers) == 1
    assert loaded.blockers[0].code == PreflightCheckCode.MISSING_DOCKERFILE
    assert len(loaded.warnings) == 1


# ---------------------------------------------------------------------------
# 3. Layer 2 — RemediationRun + FK
# ---------------------------------------------------------------------------


def test_remediation__save_and_load(db: RecoderDB) -> None:
    pre = _mk_preflight()
    pid = save_preflight_run(db, pre)
    rem = _mk_remediation(preflight_run_id=pid, proposal_id="rem_abc")
    rid = save_remediation_run(db, rem)
    loaded = load_remediation_run(db, rid)
    assert loaded is not None
    assert loaded.preflight_run_id == pid
    assert loaded.proposal_id == "rem_abc"
    assert loaded.success is True


def test_remediation__fk_violation_when_preflight_missing(db: RecoderDB) -> None:
    """존재하지 않는 preflight_run_id 로 저장 시 IntegrityError."""
    import sqlite3
    rem = _mk_remediation(preflight_run_id="nonexistent_pre", proposal_id="rem_x")
    with pytest.raises(sqlite3.IntegrityError):
        save_remediation_run(db, rem)


def test_remediation__cascade_on_preflight_delete(db: RecoderDB) -> None:
    """preflight 삭제 시 remediation 도 같이 삭제 (purge_all)."""
    pid = save_preflight_run(db, _mk_preflight())
    save_remediation_run(db, _mk_remediation(preflight_run_id=pid))
    save_remediation_run(db, _mk_remediation(preflight_run_id=pid, proposal_id="r2"))
    assert len(list_remediation_runs(db)) == 2

    db.purge_all()
    assert len(list_preflight_runs(db)) == 0
    assert len(list_remediation_runs(db)) == 0


def test_remediation__filter_by_proposal(db: RecoderDB) -> None:
    pid = save_preflight_run(db, _mk_preflight())
    save_remediation_run(db, _mk_remediation(preflight_run_id=pid, proposal_id="P1"))
    save_remediation_run(db, _mk_remediation(preflight_run_id=pid, proposal_id="P2"))
    save_remediation_run(db, _mk_remediation(preflight_run_id=pid, proposal_id="P1"))
    p1_runs = list_remediation_runs(db, proposal_id="P1")
    assert len(p1_runs) == 2


def test_remediation__success_only_filter(db: RecoderDB) -> None:
    pid = save_preflight_run(db, _mk_preflight())
    ok = _mk_remediation(preflight_run_id=pid, proposal_id="P1")
    ng = _mk_remediation(preflight_run_id=pid, proposal_id="P2")
    ng.success = False
    save_remediation_run(db, ok)
    save_remediation_run(db, ng)
    successful = list_remediation_runs(db, success_only=True)
    assert len(successful) == 1
    assert successful[0].proposal_id == "P1"


def test_remediation__statistics_by_proposal(db: RecoderDB) -> None:
    pid = save_preflight_run(db, _mk_preflight())
    for ok, rb in [(True, False), (True, False), (False, True), (False, False)]:
        r = _mk_remediation(preflight_run_id=pid, proposal_id="P1")
        r.success = ok
        r.rollback_executed = rb
        save_remediation_run(db, r)
    stats = count_remediation_runs_by_proposal(db, "P1")
    assert stats == {"total": 4, "success": 2, "failed": 2, "rolled_back": 1}


# ---------------------------------------------------------------------------
# 4. Layer 3 — DeploymentLedger (append-only + 상태 전이)
# ---------------------------------------------------------------------------


def test_deployment__save_and_load(db: RecoderDB) -> None:
    pid = save_preflight_run(db, _mk_preflight())
    dep = _mk_deployment(preflight_run_id=pid)
    did = save_deployment(db, dep)
    loaded = load_deployment(db, did)
    assert loaded is not None
    assert loaded.deployment_id == did
    assert loaded.preflight_run_id == pid
    assert loaded.status == DeploymentLedgerStatus.DEPLOYING


def test_deployment__cannot_double_insert_same_id(db: RecoderDB) -> None:
    """append-only: 같은 deployment_id 두 번 INSERT → IntegrityError."""
    import sqlite3
    dep = _mk_deployment()
    save_deployment(db, dep)
    with pytest.raises(sqlite3.IntegrityError):
        save_deployment(db, dep)


def test_deployment__status_transition_deploying_to_stable(db: RecoderDB) -> None:
    did = save_deployment(db, _mk_deployment())
    ok = update_deployment_status(
        db, did, DeploymentLedgerStatus.STABLE, health_after="healthy"
    )
    assert ok is True
    loaded = load_deployment(db, did)
    assert loaded.status == DeploymentLedgerStatus.STABLE
    assert loaded.health_after == "healthy"


def test_deployment__status_transition_deploying_to_failed(db: RecoderDB) -> None:
    did = save_deployment(db, _mk_deployment())
    ok = update_deployment_status(
        db, did, DeploymentLedgerStatus.FAILED, failure_reason="health probe timeout"
    )
    assert ok is True
    loaded = load_deployment(db, did)
    assert loaded.status == DeploymentLedgerStatus.FAILED
    assert loaded.failure_reason == "health probe timeout"


def test_deployment__invalid_transition_rejected(db: RecoderDB) -> None:
    """STABLE → STABLE 같이 잘못된 전이는 거부."""
    did = save_deployment(db, _mk_deployment())
    update_deployment_status(db, did, DeploymentLedgerStatus.STABLE)
    # STABLE → STABLE = invalid
    ok = update_deployment_status(db, did, DeploymentLedgerStatus.STABLE)
    assert ok is False


def test_deployment__terminal_rollback_no_further_transition(db: RecoderDB) -> None:
    did = save_deployment(db, _mk_deployment())
    update_deployment_status(db, did, DeploymentLedgerStatus.ROLLED_BACK)
    # ROLLED_BACK 은 terminal
    ok = update_deployment_status(db, did, DeploymentLedgerStatus.STABLE)
    assert ok is False


def test_deployment__stable_to_rolled_back_allowed(db: RecoderDB) -> None:
    did = save_deployment(db, _mk_deployment())
    update_deployment_status(db, did, DeploymentLedgerStatus.STABLE)
    ok = update_deployment_status(db, did, DeploymentLedgerStatus.ROLLED_BACK)
    assert ok is True


def test_deployment__update_nonexistent_returns_false(db: RecoderDB) -> None:
    ok = update_deployment_status(db, "missing_id", DeploymentLedgerStatus.STABLE)
    assert ok is False


def test_deployment__list_filter_status(db: RecoderDB) -> None:
    d1 = save_deployment(db, _mk_deployment())
    d2 = save_deployment(db, _mk_deployment())
    update_deployment_status(db, d1, DeploymentLedgerStatus.STABLE)
    update_deployment_status(db, d2, DeploymentLedgerStatus.FAILED)
    stables = list_deployments(db, status=DeploymentLedgerStatus.STABLE)
    failures = list_deployments(db, status=DeploymentLedgerStatus.FAILED)
    assert len(stables) == 1 and stables[0].deployment_id == d1
    assert len(failures) == 1 and failures[0].deployment_id == d2


def test_deployment__rollback_candidates_returns_only_stable(db: RecoderDB) -> None:
    for _ in range(3):
        d = save_deployment(db, _mk_deployment(project_id="A"))
        update_deployment_status(db, d, DeploymentLedgerStatus.STABLE)
    # 1 failed
    df = save_deployment(db, _mk_deployment(project_id="A"))
    update_deployment_status(db, df, DeploymentLedgerStatus.FAILED)
    candidates = list_rollback_candidates(db, project_id="A", limit=10)
    assert len(candidates) == 3
    assert all(c.status == DeploymentLedgerStatus.STABLE for c in candidates)


def test_deployment__roundtrip_preserves_metadata(db: RecoderDB) -> None:
    dep = _mk_deployment()
    dep.metadata = {"region": "ap-northeast-2", "deployer_id": "user_xxx"}
    did = save_deployment(db, dep)
    loaded = load_deployment(db, did)
    assert loaded.metadata["region"] == "ap-northeast-2"


# ---------------------------------------------------------------------------
# 5. Concurrency — WAL mode supports multi-reader + single-writer
# ---------------------------------------------------------------------------


def test_concurrent_reads_while_writing(db: RecoderDB) -> None:
    """동시 읽기와 쓰기 시 deadlock / lock 에러 없어야 함."""
    # Pre-populate
    for _ in range(5):
        save_preflight_run(db, _mk_preflight())

    errors: list[str] = []

    def reader() -> None:
        try:
            for _ in range(10):
                _ = list_preflight_runs(db, limit=100)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"reader: {exc!r}")

    def writer() -> None:
        try:
            for _ in range(10):
                save_preflight_run(db, _mk_preflight())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"writer: {exc!r}")

    threads = [threading.Thread(target=reader) for _ in range(2)] + [threading.Thread(target=writer)]
    # WAL 모드 + check_same_thread=True 일 때는 각 thread 가 별도 connection 만들어야 안전
    # connect 마다 새 connection 을 만드는 우리 매니저 구조라 OK
    # 하지만 check_same_thread=True 라 같은 connection 공유는 못 함.
    # 우리 RecoderDB 는 매번 new connection 이므로 thread-safe.
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == [], errors
