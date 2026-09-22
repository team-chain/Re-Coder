"""
디바이스 등록 부트스트랩 — Codex P1 2건 회귀 테스트.

1. 조직의 첫 사용자 등록이 OrgMember 를 **실제로 저장**해야 한다.
   저장하지 않으면 토큰은 발급되는데 이후 모든 요청이
   get_member_role() == None 으로 거부돼, 첫 디바이스가 발급 즉시
   쓸 수 없는 토큰을 쥔다.
2. 토큰 → 디바이스 조회는 RLS 밖 부트스트랩 경로를 쓰되,
   비 PostgreSQL 백엔드에서는 직접 SELECT 로 폴백해 동작해야 한다.

실행:  pytest control_plane/tests/test_auth_bootstrap.py -q  (repo 루트에서)
"""
import asyncio
import os
import sys

# session.py 가 임포트 시점에 엔진을 만든다 — asyncpg 없이도 임포트되도록
# 테스트는 sqlite URL 로 미리 덮는다.
os.environ.setdefault("CONTROL_PLANE_DATABASE_URL", "sqlite+aiosqlite://")
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from control_plane.db.models import Base, Organization, OrgMember, User  # noqa: E402
from control_plane.models.schemas import DeviceEnrollRequest, OrgRole  # noqa: E402


class _StubRequest:
    client = None


def _run(coro):
    return asyncio.run(coro)


async def _fresh_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_first_enrollment_persists_org_membership():
    """[Codex P1 회귀] 첫 등록 후 OrgMember 행이 존재하고, 발급된 토큰이
    이후 요청의 role 조회까지 통과한다."""
    from control_plane.api.routes import auth as auth_route
    from control_plane.services.identity import IdentityService
    from control_plane.services.org_service import OrgService

    async def _scenario():
        engine, Session = await _fresh_session()
        async with Session() as db:
            user = User(email="first@x.io", display_name="첫 사용자", is_active=True,
                        oidc_provider="github", oidc_subject="u1")
            org = Organization(name="Org", slug="org")
            db.add_all([user, org])
            await db.flush()

            # temp_token 준비 (라우트가 읽는 인메모리 스토어)
            auth_route._TEMP_TOKEN_STORE.set("tt", {
                "user_id": user.user_id,
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            })
            body = auth_route.EnrollDeviceBody(
                temp_token="tt",
                org_id=org.org_id,
                enroll=DeviceEnrollRequest(
                    display_name="d", os_type="linux",
                    vscode_version="1", extension_version="1"),
            )
            token_resp = await auth_route.enroll_device(body, _StubRequest(), db)

            # 1) 멤버십이 **저장**됐는가 — 이게 이 P1 의 본판이다.
            role = await OrgService(db).get_member_role(org.org_id, user.user_id)
            assert role == OrgRole.DEVELOPER, (
                "첫 등록이 OrgMember 를 저장하지 않았다 — 토큰이 발급 즉시 무용지물"
            )

            # 2) 발급된 토큰이 검증 경로를 실제로 통과하는가 (sqlite 폴백 경로).
            device = await IdentityService(db).validate_token(token_resp.token)
            assert device is not None and device.org_id == org.org_id
        await engine.dispose()

    _run(_scenario())


def test_first_enrollment_locks_organization_row():
    """The empty-organization claim must compile to a PostgreSQL row lock."""
    from sqlalchemy.dialects import postgresql
    from control_plane.api.routes.auth import _organization_bootstrap_lock

    compiled = str(
        _organization_bootstrap_lock("org-1").compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "FOR UPDATE" in compiled
    assert "organizations.org_id = 'org-1'" in compiled


def test_missing_postgres_bootstrap_function_uses_savepoint_fallback(monkeypatch):
    """A failed optional function probe must not abort the fallback transaction."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession
    from control_plane.services.identity import IdentityService

    async def _scenario():
        engine, Session = await _fresh_session()
        async with Session() as db:
            user = User(email="fallback@x.io", display_name="fallback", is_active=True,
                        oidc_provider="github", oidc_subject="fallback-user")
            org = Organization(name="Fallback Org", slug="fallback-org")
            db.add_all([user, org])
            await db.flush()

            service = IdentityService(db)
            issued = await service.enroll_device(
                user_id=user.user_id,
                org_id=org.org_id,
                role=OrgRole.DEVELOPER,
                request=DeviceEnrollRequest(
                    display_name="device", os_type="linux",
                    vscode_version="1", extension_version="1",
                ),
            )

            savepoints = []
            original_begin_nested = AsyncSession.begin_nested

            def _begin_nested_spy(session):
                savepoints.append(True)
                return original_begin_nested(session)

            monkeypatch.setattr(AsyncSession, "begin_nested", _begin_nested_spy)
            monkeypatch.setattr(db.get_bind().dialect, "name", "postgresql")

            # SQLite has no auth_device_by_token_hash function. The simulated
            # PostgreSQL branch therefore fails its probe, rolls back the nested
            # transaction, and authenticates through the direct-query fallback.
            device = await service._get_device_by_token(issued.token)
            assert device is not None and device.device_id == issued.device_id
            assert len(savepoints) == 1

            # The surrounding transaction remains usable after the failed probe.
            assert (await db.execute(
                select(User).where(User.user_id == user.user_id)
            )).scalar_one() is user
        await engine.dispose()

    _run(_scenario())


def test_second_user_still_requires_invite():
    """[음성 대조] 멤버가 이미 있는 조직엔 자동 등록이 없다 — 403."""
    from fastapi import HTTPException
    from control_plane.api.routes import auth as auth_route

    async def _scenario():
        engine, Session = await _fresh_session()
        async with Session() as db:
            owner = User(email="own@x.io", display_name="o", is_active=True,
                         oidc_provider="github", oidc_subject="u2")
            intruder = User(email="in@x.io", display_name="i", is_active=True,
                            oidc_provider="github", oidc_subject="u3")
            org = Organization(name="Org", slug="org2")
            db.add_all([owner, intruder, org])
            await db.flush()
            db.add(OrgMember(org_id=org.org_id, user_id=owner.user_id, role=OrgRole.OWNER))
            await db.flush()

            auth_route._TEMP_TOKEN_STORE.set("tt2", {
                "user_id": intruder.user_id,
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            })
            body = auth_route.EnrollDeviceBody(
                temp_token="tt2", org_id=org.org_id,
                enroll=DeviceEnrollRequest(
                    display_name="d", os_type="linux",
                    vscode_version="1", extension_version="1"),
            )
            with pytest.raises(HTTPException) as exc:
                await auth_route.enroll_device(body, _StubRequest(), db)
            assert exc.value.status_code == 403
        await engine.dispose()

    _run(_scenario())
