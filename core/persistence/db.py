"""
SQLite connection manager for ReCoder 3-Layer persistence (§33).

설계:
  - 단일 sqlite3 파일. WAL 모드 (동시 읽기 다수 + 쓰기 1).
  - foreign_keys = ON (Pydantic 의 FK 의미를 DB 레벨에서도 강제).
  - 모든 datetime 은 ISO-8601 UTC TEXT — SQLite 의 ``DATETIME`` 표면 호환.
  - DDL 은 ``DDL`` 상수에 명시 (마이그레이션은 v0 — 한 번만 만들고 끝).
  - thread-safety: 하나의 ``RecoderDB`` 인스턴스를 thread 마다 ``connect()`` 호출.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


def get_default_db_path(workspace: Optional[Path | str] = None) -> Path:
    """기본 DB 경로: ``<workspace>/.recoder/recoder.db``.

    env ``RECODER_DB_PATH`` 가 있으면 그쪽이 우선.
    workspace 미지정 시 CWD 사용.
    """
    env = os.environ.get("RECODER_DB_PATH")
    if env:
        return Path(env).expanduser().resolve()
    base = Path(workspace) if workspace else Path.cwd()
    return (base / ".recoder" / "recoder.db").resolve()


# ---------------------------------------------------------------------------
# DDL — 한 번만 실행. 추가 컬럼은 ALTER TABLE 로 별도 마이그레이션 (v1+).
# ---------------------------------------------------------------------------


DDL: str = """
CREATE TABLE IF NOT EXISTS preflight_runs (
    preflight_run_id   TEXT PRIMARY KEY,
    project_id         TEXT,
    contract_hash      TEXT,
    status             TEXT NOT NULL,
    score              INTEGER NOT NULL DEFAULT 0,
    payload            TEXT NOT NULL,           -- PreflightRun.model_dump_json()
    created_at         TEXT NOT NULL            -- ISO-8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_preflight_project   ON preflight_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_preflight_status    ON preflight_runs(status);
CREATE INDEX IF NOT EXISTS idx_preflight_created   ON preflight_runs(created_at DESC);


CREATE TABLE IF NOT EXISTS remediation_runs (
    remediation_run_id  TEXT PRIMARY KEY,
    preflight_run_id    TEXT NOT NULL,
    proposal_id         TEXT NOT NULL,
    success             INTEGER NOT NULL DEFAULT 0,  -- 0=false / 1=true
    rollback_executed   INTEGER NOT NULL DEFAULT 0,
    payload             TEXT NOT NULL,
    applied_at          TEXT NOT NULL,
    FOREIGN KEY (preflight_run_id) REFERENCES preflight_runs(preflight_run_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_remediation_preflight ON remediation_runs(preflight_run_id);
CREATE INDEX IF NOT EXISTS idx_remediation_proposal  ON remediation_runs(proposal_id);
CREATE INDEX IF NOT EXISTS idx_remediation_applied   ON remediation_runs(applied_at DESC);


CREATE TABLE IF NOT EXISTS deployments (
    deployment_id      TEXT PRIMARY KEY,
    project_id         TEXT,
    preflight_run_id   TEXT,
    contract_hash      TEXT,
    git_commit         TEXT,
    image_digest       TEXT,
    status             TEXT NOT NULL,
    payload            TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    FOREIGN KEY (preflight_run_id) REFERENCES preflight_runs(preflight_run_id)
        ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_deployments_project ON deployments(project_id);
CREATE INDEX IF NOT EXISTS idx_deployments_status  ON deployments(status);
CREATE INDEX IF NOT EXISTS idx_deployments_created ON deployments(created_at DESC);
"""


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------


class RecoderDB:
    """SQLite 연결 관리자.

    Args:
        db_path: SQLite 파일 경로. 없으면 생성.
        check_same_thread: 기본 False (FastAPI 멀티스레드 핸들러 지원).
            테스트에서는 True 권장.
    """

    def __init__(self, db_path: Path | str, *, check_same_thread: bool = False) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._check_same_thread = check_same_thread
        self._init_db()

    # ------------------------------------------------------------------
    # 내부 — 최초 1회 DDL 실행 + PRAGMA 설정
    # ------------------------------------------------------------------

    def _connect_raw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=self._check_same_thread,
            isolation_level=None,  # autocommit; 명시적 BEGIN/COMMIT 권장
        )
        conn.row_factory = sqlite3.Row
        # PRAGMA — 매 연결마다 명시. WAL/FK 는 매 connection scope.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect_raw() as conn:
            conn.executescript(DDL)

    # ------------------------------------------------------------------
    # 외부 — context manager 로 안전한 연결 사용
    # ------------------------------------------------------------------

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """단일 작업 단위 연결. with 블록 종료 시 자동 close.

        예시:
            with db.connect() as conn:
                conn.execute("INSERT ...", (...))
                conn.execute("COMMIT")
        """
        conn = self._connect_raw()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """명시적 트랜잭션. 예외 시 자동 ROLLBACK."""
        conn = self._connect_raw()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # WAL 정리 — Windows 에서 TemporaryDirectory cleanup 시 파일 잠금 해소용.
    # ------------------------------------------------------------------

    def checkpoint_and_close_wal(self) -> None:
        """WAL 보조 파일 (.db-wal, .db-shm) 을 강제로 main DB 에 머지 + 삭제.

        Windows 에서 SQLite WAL 모드 사용 시 .db-wal / .db-shm 가 잠긴 채
        남을 수 있어 임시 디렉토리 cleanup 실패 (WinError 32) 가 흔하다.
        평가/테스트 종료 시 호출 권장.
        """
        try:
            with self._connect_raw() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode = DELETE")
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------
    # 운영 — 테스트에서만 사용. Production 호출 금지.
    # ------------------------------------------------------------------

    def purge_all(self) -> None:
        """모든 테이블 데이터 삭제. 테스트용. **DeploymentLedger 의 append-only 약속을 깨므로 production 사용 금지.**"""
        with self.transaction() as conn:
            for tbl in ("remediation_runs", "deployments", "preflight_runs"):
                conn.execute(f"DELETE FROM {tbl}")
