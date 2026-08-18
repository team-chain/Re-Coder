"""
Control Plane — DB 초기화 + RLS 정책 적용

설계서 §Q2-A2:
- PostgreSQL Row Level Security를 추가 안전장치로 적용
- AuditLog UPDATE/DELETE 금지 트리거

실제 운영에서는 Alembic migration으로 관리하지만,
개발/테스트 환경에서는 이 스크립트로 초기화한다.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.db.models import Base
from control_plane.db.session import engine

logger = logging.getLogger(__name__)

# RLS 정책: org_id 기반 행 격리
_RLS_POLICIES = [
    # devices
    "ALTER TABLE devices ENABLE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS devices_org_isolation ON devices",
    "CREATE POLICY devices_org_isolation ON devices USING (org_id = current_setting('app.current_org_id', true)::text)",
    # workspaces
    "ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS workspaces_org_isolation ON workspaces",
    "CREATE POLICY workspaces_org_isolation ON workspaces USING (org_id = current_setting('app.current_org_id', true)::text)",
    # projects
    "ALTER TABLE projects ENABLE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS projects_org_isolation ON projects",
    "CREATE POLICY projects_org_isolation ON projects USING (org_id = current_setting('app.current_org_id', true)::text)",
    # audit_events (읽기만 격리 — 쓰기는 서비스 레이어에서 강제)
    "ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS audit_org_isolation ON audit_events",
    "CREATE POLICY audit_org_isolation ON audit_events USING (org_id = current_setting('app.current_org_id', true)::text)",
]

# Each table's ENABLE/DROP/CREATE sequence is one replacement unit. A failure
# after DROP must roll back the whole unit and preserve the previous policy.
_RLS_POLICY_GROUPS = [
    ("devices", _RLS_POLICIES[0:3]),
    ("workspaces", _RLS_POLICIES[3:6]),
    ("projects", _RLS_POLICIES[6:9]),
    ("audit_events", _RLS_POLICIES[9:12]),
]

# ── 인증 부트스트랩: RLS 밖에서 토큰 → 디바이스 조회 ────────────────
#
# 닭-달걀 문제: RLS 정책은 `app.current_org_id` 를 요구하는데, org_id 는
# **디바이스를 찾아야** 알 수 있다. 애플리케이션 롤이 RLS 대상이 되는 순간
# validate_token() 의 devices 조회가 0행이 되어 모든 유효 토큰이 401 이 된다.
#
# 해법: 테이블 소유자 권한으로 도는 SECURITY DEFINER 함수 하나만 예외로
# 열어 준다. 이 함수는 **정확한 token_hash 일치**로만 조회하므로, 실행
# 권한이 있어도 해시 원문 없이는 아무 행도 얻을 수 없다. 인증이 끝나면
# 미들웨어가 org 컨텍스트를 설정하고, 이후 모든 조회는 RLS 아래에서 돈다.
_AUTH_BOOTSTRAP_FUNCTION = """
CREATE OR REPLACE FUNCTION auth_device_by_token_hash(p_token_hash text)
RETURNS SETOF devices
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = public
AS $$
    SELECT * FROM devices WHERE token_hash = p_token_hash
$$;
"""

# AuditLog 불변성: UPDATE/DELETE 금지 트리거.
#
# **문장을 쪼개 둔 이유**: asyncpg 는 prepared statement 기반이라 한 execute()
# 에 여러 SQL 문장을 넣으면 준비 단계에서 거부한다. 단, 함수 교체→기존 트리거
# 제거→새 트리거 생성은 하나의 원자적 변경이어야 한다. 세 번 execute 하되 같은
# 트랜잭션에서 실행해 마지막 문장이 실패하면 기존 트리거까지 복원한다.
_AUDIT_IMMUTABILITY_STATEMENTS = [
    """
CREATE OR REPLACE FUNCTION prevent_audit_modification()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'AuditLog is immutable: UPDATE and DELETE are not permitted on audit_events';
END;
$$ LANGUAGE plpgsql
""",
    "DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events",
    """
CREATE TRIGGER audit_events_no_update
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification()
""",
]


async def _hash_version_column_exists() -> bool:
    """Return whether the required audit hash-version column is present."""
    def _has_column(sync_conn) -> bool:
        from sqlalchemy import inspect as _inspect
        insp = _inspect(sync_conn)
        cols = {c["name"] for c in insp.get_columns("audit_events")}
        return "hash_version" in cols

    async with engine.connect() as conn:
        return await conn.run_sync(_has_column)


async def _migrate_hash_version_column() -> None:
    """Add audit_events.hash_version or fail startup if it remains absent."""
    if await _hash_version_column_exists():
        return

    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "ALTER TABLE audit_events ADD COLUMN hash_version INTEGER NOT NULL DEFAULT 1")
        logger.info("audit_events.hash_version 컬럼 추가")
    except Exception as exc:  # noqa: BLE001
        # A concurrent initializer may have won the ALTER race. Suppress the
        # error only after a fresh transaction proves the column now exists.
        try:
            created_concurrently = await _hash_version_column_exists()
        except Exception:  # noqa: BLE001
            raise RuntimeError(
                "audit_events.hash_version migration failed and could not be verified"
            ) from exc
        if created_concurrently:
            logger.info("audit_events.hash_version was created concurrently")
            return
        raise RuntimeError(
            "audit_events.hash_version migration failed and the column is still absent"
        ) from exc


async def _execute_ddl(label: str, stmt: str) -> bool:
    """DDL 한 문장을 **독립 트랜잭션**에서 실행한다.

    PostgreSQL 은 트랜잭션 안에서 문장 하나가 실패하면 그 뒤 모든 문장을
    InFailedSQLTransaction 으로 거부한다. 예전처럼 하나의 engine.begin()
    안에서 try/except 로 넘기면 — 첫 실패가 남은 셋업 전체를 조용히
    무효화하고, 커밋 시점 롤백으로 **테이블 생성까지 되돌아갈 수 있다.**
    """
    try:
        async with engine.begin() as conn:
            await conn.execute(text(stmt))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s skipped (%s): %s", label, stmt.strip()[:60], exc)
        return False


async def _execute_ddl_group_atomic(label: str, statements: list[str]) -> bool:
    """Execute separate asyncpg-compatible statements in one transaction."""
    try:
        async with engine.begin() as conn:
            for stmt in statements:
                await conn.execute(text(stmt))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "%s rolled back after DDL failure: %s",
            label,
            exc,
        )
        return False


async def _apply_rls_policies() -> bool:
    """Replace each table's RLS policy in one transaction."""
    results = []
    for table_name, statements in _RLS_POLICY_GROUPS:
        results.append(await _execute_ddl_group_atomic(
            f"RLS policy ({table_name})",
            statements,
        ))
    return all(results)


async def init_db(apply_rls: bool = True) -> None:
    """테이블 생성 + RLS + AuditLog 불변성 트리거 적용"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        logger.info("Tables created")

    # 구 스키마 마이그레이션 — audit_events.hash_version 이 없으면 추가.
    # **컬럼 존재를 먼저 확인**한 뒤, 없을 때만 **독립 트랜잭션**에서 ALTER 한다.
    #   - create_all 이 신규 DB 엔 이미 이 컬럼을 만든다. 무조건 ADD COLUMN 하면
    #     중복 컬럼 오류가 나고, PostgreSQL 은 그 오류로 트랜잭션이 abort 되어
    #     같은 트랜잭션의 create_all 커밋까지 실패한다(뒤의 RLS 셋업도 스킵).
    #   - 그래서 존재 확인 → 없으면 자기 트랜잭션에서만 ALTER. 실패해도
    #     초기화 전체가 죽지 않는다.
    await _migrate_hash_version_column()

    if apply_rls:
        await _apply_rls_policies()

        # asyncpg 호환을 위해 문장별 execute를 유지하되, 교체 작업 전체는 한
        # 트랜잭션으로 묶어 CREATE TRIGGER 실패 시 기존 트리거를 복원한다.
        trigger_ok = await _execute_ddl_group_atomic(
            "Audit immutability",
            _AUDIT_IMMUTABILITY_STATEMENTS,
        )
        if trigger_ok:
            logger.info("AuditLog immutability trigger applied")

        # RLS 를 켰다면 인증 부트스트랩 함수도 함께 있어야 한다 —
        # 없으면 토큰 검증이 RLS 에 막혀 모든 요청이 401 이 된다.
        if await _execute_ddl("Auth bootstrap function", _AUTH_BOOTSTRAP_FUNCTION):
            logger.info("Auth bootstrap function applied")
