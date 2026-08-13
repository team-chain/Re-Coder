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

# AuditLog 불변성: UPDATE/DELETE 금지 트리거
_AUDIT_IMMUTABILITY_TRIGGER = """
CREATE OR REPLACE FUNCTION prevent_audit_modification()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'AuditLog is immutable: UPDATE and DELETE are not permitted on audit_events';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events;
CREATE TRIGGER audit_events_no_update
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();
"""


async def init_db(apply_rls: bool = True) -> None:
    """테이블 생성 + RLS + AuditLog 불변성 트리거 적용"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        logger.info("Tables created")

        if apply_rls:
            for stmt in _RLS_POLICIES:
                try:
                    await conn.execute(text(stmt))
                except Exception as exc:
                    logger.warning("RLS policy skipped (%s): %s", stmt[:60], exc)

            try:
                await conn.execute(text(_AUDIT_IMMUTABILITY_TRIGGER))
                logger.info("AuditLog immutability trigger applied")
            except Exception as exc:
                logger.warning("Audit immutability trigger skipped: %s", exc)

            # RLS 를 켰다면 인증 부트스트랩 함수도 함께 있어야 한다 —
            # 없으면 토큰 검증이 RLS 에 막혀 모든 요청이 401 이 된다.
            try:
                await conn.execute(text(_AUTH_BOOTSTRAP_FUNCTION))
                logger.info("Auth bootstrap function applied")
            except Exception as exc:
                logger.warning("Auth bootstrap function skipped: %s", exc)
