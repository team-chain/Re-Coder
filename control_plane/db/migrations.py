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
# 에 여러 SQL 문장을 넣으면 준비 단계에서 거부한다. 게다가 한 트랜잭션 안에서
# 실패한 문장 뒤의 모든 문장은 InFailedSQLTransaction 으로 연쇄 실패하므로,
# 각 문장은 **자기 트랜잭션**에서 실행해야 하나가 죽어도 나머지가 산다.
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


async def init_db(apply_rls: bool = True) -> None:
    """테이블 생성 + RLS + AuditLog 불변성 트리거 적용"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        logger.info("Tables created")

        # 구 스키마 마이그레이션 — audit_events.hash_version 이 없으면 추가.
        # 기존 행은 default 1(v1 부분 페이로드 포맷)로 남고, 신규 행은 v2 로
        # 기록된다. 검증기가 행 버전에 맞춰 재계산하므로 옛 행이 위조로
        # 오판되지 않는다.
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE audit_events ADD COLUMN hash_version INTEGER NOT NULL DEFAULT 1")
            logger.info("audit_events.hash_version 컬럼 추가")
        except Exception as exc:  # noqa: BLE001 — 이미 있으면 무시
            logger.debug("hash_version 마이그레이션 스킵: %s", exc)

    if apply_rls:
        for stmt in _RLS_POLICIES:
            await _execute_ddl("RLS policy", stmt)

        # asyncpg 는 한 execute() 에 여러 문장을 허용하지 않는다 — 문장 단위로.
        trigger_ok = all([
            await _execute_ddl("Audit immutability", stmt)
            for stmt in _AUDIT_IMMUTABILITY_STATEMENTS
        ])
        if trigger_ok:
            logger.info("AuditLog immutability trigger applied")

        # RLS 를 켰다면 인증 부트스트랩 함수도 함께 있어야 한다 —
        # 없으면 토큰 검증이 RLS 에 막혀 모든 요청이 401 이 된다.
        if await _execute_ddl("Auth bootstrap function", _AUTH_BOOTSTRAP_FUNCTION):
            logger.info("Auth bootstrap function applied")
