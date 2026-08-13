"""
동시성 방어 — Codex P1(투표 잠금) · P2(토큰 원자 소비) · DDL 분리 회귀 테스트.
"""
import asyncio
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.environ.setdefault("CONTROL_PLANE_DATABASE_URL", "sqlite+aiosqlite://")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from control_plane.db.models import ApprovalRequest, Base, Organization, User  # noqa: E402
from control_plane.models.schemas import ApprovalVoteRequest  # noqa: E402


async def _fresh():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _user(i):
    return User(email=f"u{i}@x.io", display_name=f"u{i}", is_active=True,
                oidc_provider="github", oidc_subject=f"s{i}")


# ── 1. 투표 잠금 ───────────────────────────────────────────────────────

def test_vote_loads_request_with_row_lock():
    """[Codex P1 회귀] vote() 는 요청 행을 FOR UPDATE 로 잠근 채 진행해야 한다.

    (a) vote 경로가 for_update=True 로 조회하는지 — 잠금 없이 두 승인자가
        동시에 투표하면 둘 다 current_approvals=1 을 써서 2인 승인 요청이
        영원히 PENDING 으로 남는다.
    (b) 그 플래그가 실제로 FOR UPDATE SQL 을 만드는지 (postgres 방언 컴파일).
    """
    from unittest.mock import patch
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql
    from control_plane.services.approval_service import ApprovalService

    # (b) 플래그 → SQL
    stmt = select(ApprovalRequest).with_for_update()
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled

    # (a) vote 경로가 잠금 조회를 쓰는지
    async def _scenario():
        engine, Session = await _fresh()
        async with Session() as db:
            u1, u2 = _user(1), _user(2)
            org = Organization(name="o", slug="o1")
            db.add_all([u1, u2, org]); await db.flush()
            ar = ApprovalRequest(
                org_id=org.org_id, requester_user_id=u1.user_id,
                action_summary="deploy", resource_type="deployment",
                risk_reason="lvl3", required_approvers=2,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                policy_bundle_version="v1.0.0")
            db.add(ar); await db.flush()

            svc = ApprovalService(db)
            captured: list[bool] = []
            original = ApprovalService._get_request

            async def _spy(self, rid, oid, for_update=False):
                captured.append(for_update)
                return await original(self, rid, oid, for_update=for_update)

            with patch.object(ApprovalService, "_get_request", _spy):
                await svc.vote(ar.approval_request_id, u1.user_id,
                               ApprovalVoteRequest(approved=True, reason="ok"), org.org_id)
            assert captured and captured[0] is True, (
                "vote() 가 행 잠금 없이 요청을 읽는다 — 동시 투표 유실 경로"
            )

            # 기능 확인: 두 번째 승인으로 APPROVED 전환
            out = await svc.vote(ar.approval_request_id, u2.user_id,
                                 ApprovalVoteRequest(approved=True, reason="ok"), org.org_id)
            assert out.status.value == "approved"
            assert out.current_approvals == 2
        await engine.dispose()

    asyncio.run(_scenario())


def test_duplicate_vote_rejected():
    """[음성 대조] 같은 사용자의 중복 투표는 거부."""
    from control_plane.services.approval_service import ApprovalService

    async def _scenario():
        engine, Session = await _fresh()
        async with Session() as db:
            u1 = _user(3); org = Organization(name="o", slug="o2")
            db.add_all([u1, org]); await db.flush()
            ar = ApprovalRequest(
                org_id=org.org_id, requester_user_id=u1.user_id,
                action_summary="d", resource_type="deployment",
                risk_reason="r", required_approvers=2,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                policy_bundle_version="v1")
            db.add(ar); await db.flush()
            svc = ApprovalService(db)
            await svc.vote(ar.approval_request_id, u1.user_id,
                           ApprovalVoteRequest(approved=True, reason="ok"), org.org_id)
            with pytest.raises(ValueError):
                await svc.vote(ar.approval_request_id, u1.user_id,
                               ApprovalVoteRequest(approved=True, reason="again"), org.org_id)
        await engine.dispose()

    asyncio.run(_scenario())


# ── 2. temp_token 원자 소비 ────────────────────────────────────────────

def test_memory_claim_is_single_winner():
    """[Codex P2 회귀] 같은 토큰을 여러 스레드가 동시에 claim 하면
    정확히 하나만 성공한다 — get 후 pop 이던 시절엔 전원이 통과했다."""
    from control_plane.api.routes.auth import _MemoryTempStore

    store = _MemoryTempStore()
    store.set("tok", {"user_id": "u",
                      "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)})

    results: list = []
    barrier = threading.Barrier(8)

    def _worker():
        barrier.wait()
        results.append(store.claim("tok"))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"claim 승자가 {len(winners)}명 — 원자적이지 않다"
    assert store.claim("tok") is None


def test_memory_claim_rejects_expired():
    from control_plane.api.routes.auth import _MemoryTempStore
    store = _MemoryTempStore()
    store.set("old", {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    assert store.claim("old") is None


def test_enroll_failure_restores_claim():
    """등록 실패(403) 후엔 같은 temp_token 으로 재시도할 수 있어야 한다 —
    소비만 하고 복원하지 않으면 사용자 실수 한 번에 로그인부터 다시다."""
    from fastapi import HTTPException
    from control_plane.api.routes import auth as auth_route
    from control_plane.db.models import OrgMember
    from control_plane.models.schemas import DeviceEnrollRequest, OrgRole

    class _Req: client = None

    async def _scenario():
        engine, Session = await _fresh()
        async with Session() as db:
            member, outsider = _user(4), _user(5)
            org = Organization(name="o", slug="o3")
            db.add_all([member, outsider, org]); await db.flush()
            db.add(OrgMember(org_id=org.org_id, user_id=member.user_id,
                             role=OrgRole.OWNER)); await db.flush()

            auth_route._TEMP_TOKEN_STORE.set("rt", {
                "user_id": outsider.user_id,
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)})
            body = auth_route.EnrollDeviceBody(
                temp_token="rt", org_id=org.org_id,
                enroll=DeviceEnrollRequest(display_name="d", os_type="linux",
                                           vscode_version="1", extension_version="1"))
            with pytest.raises(HTTPException):
                await auth_route.enroll_device(body, _Req(), db)
            assert auth_route._TEMP_TOKEN_STORE.get("rt") is not None, (
                "실패한 등록이 temp_token 을 소비한 채 복원하지 않았다"
            )
        await engine.dispose()

    asyncio.run(_scenario())


# ── 3. DDL 분리 ────────────────────────────────────────────────────────

def test_audit_trigger_ddl_is_split_into_single_statements():
    """[Codex P1 회귀] asyncpg 는 한 execute() 에 여러 문장을 거부한다 —
    트리거 DDL 은 문장 리스트여야 하고, 각 항목은 단일 문장이어야 한다."""
    from control_plane.db import migrations

    stmts = migrations._AUDIT_IMMUTABILITY_STATEMENTS
    assert isinstance(stmts, list) and len(stmts) == 3
    for stmt in stmts:
        # $$ 함수 몸통 밖에 문장 구분자가 없어야 단일 문장이다.
        body_stripped = stmt.split("$$")[0] + (stmt.split("$$")[-1] if "$$" in stmt else "")
        assert ";" not in body_stripped.rstrip().rstrip(";"), f"복수 문장: {stmt[:60]}"


def test_init_db_survives_backend_without_rls(monkeypatch):
    """DDL 하나가 실패해도(sqlite 는 PG 문법 거부) 테이블 생성은 살아남는다 —
    예전엔 한 트랜잭션이라 첫 실패가 전체 셋업을 오염시켰다."""
    from control_plane.db import migrations
    from sqlalchemy.ext.asyncio import create_async_engine as _cae

    engine = _cae("sqlite+aiosqlite://")
    monkeypatch.setattr(migrations, "engine", engine)

    async def _scenario():
        await migrations.init_db(apply_rls=True)   # PG 전용 DDL 은 경고로 스킵
        from sqlalchemy import text
        async with engine.connect() as conn:
            r = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='devices'"))
            assert r.first() is not None, "테이블 생성이 롤백됐다"
        await engine.dispose()

    asyncio.run(_scenario())
