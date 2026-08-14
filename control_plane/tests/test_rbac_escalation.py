"""
org RBAC — Codex P1: 권한 상승 방지 회귀.
1. DEVELOPER 는 멤버를 초대할 수 없다(MEMBER_MANAGE 없음).
2. ADMIN 은 자기를 OWNER 로 승격할 수 없다(역할 상한).
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.environ.setdefault("CONTROL_PLANE_DATABASE_URL", "sqlite+aiosqlite://")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from control_plane.db.models import Base, Organization, OrgMember, User  # noqa: E402
from control_plane.models.schemas import (  # noqa: E402
    OrgMemberInvite, OrgRole, Permission, ROLE_PERMISSIONS, RoleChangeRequest,
)
from control_plane.services.org_service import OrgService  # noqa: E402


async def _fresh():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _u(i, **kw):
    return User(email=f"u{i}@x.io", display_name=f"u{i}", is_active=True,
                oidc_provider="github", oidc_subject=f"s{i}", **kw)


def test_developer_cannot_manage_members():
    """[Codex P1 회귀] DEVELOPER 에게 MEMBER_MANAGE 가 없어 초대가 거부된다."""
    assert Permission.MEMBER_MANAGE not in ROLE_PERMISSIONS[OrgRole.DEVELOPER]

    async def _s():
        engine, S = await _fresh()
        async with S() as db:
            dev, target = _u(1), _u(2)
            org = Organization(name="o", slug="e1")
            db.add_all([dev, target, org]); await db.flush()
            db.add(OrgMember(org_id=org.org_id, user_id=dev.user_id,
                             role=OrgRole.DEVELOPER)); await db.flush()
            svc = OrgService(db)
            with pytest.raises(PermissionError):
                await svc.add_member(org.org_id, dev.user_id,
                    OrgMemberInvite(email=target.email, role=OrgRole.OWNER))
        await engine.dispose()
    asyncio.run(_s())


def test_admin_cannot_self_escalate_to_owner():
    """[Codex P1 회귀] ADMIN 이 자기를 OWNER 로 올리려 하면 상한에 걸린다."""
    async def _s():
        engine, S = await _fresh()
        async with S() as db:
            admin = _u(3)
            org = Organization(name="o", slug="e2")
            db.add_all([admin, org]); await db.flush()
            db.add(OrgMember(org_id=org.org_id, user_id=admin.user_id,
                             role=OrgRole.ADMIN)); await db.flush()
            svc = OrgService(db)
            with pytest.raises(PermissionError):
                await svc.change_role(org.org_id, admin.user_id,
                    RoleChangeRequest(user_id=admin.user_id, new_role=OrgRole.OWNER, reason="x"))
        await engine.dispose()
    asyncio.run(_s())


def test_admin_can_still_manage_lower_roles():
    """[음성 대조] ADMIN 은 DEVELOPER 초대·역할 변경은 정상적으로 할 수 있다."""
    async def _s():
        engine, S = await _fresh()
        async with S() as db:
            admin, target = _u(4), _u(5)
            org = Organization(name="o", slug="e3")
            db.add_all([admin, target, org]); await db.flush()
            db.add(OrgMember(org_id=org.org_id, user_id=admin.user_id,
                             role=OrgRole.ADMIN)); await db.flush()
            svc = OrgService(db)
            out = await svc.add_member(org.org_id, admin.user_id,
                OrgMemberInvite(email=target.email, role=OrgRole.DEVELOPER))
            assert out.role == OrgRole.DEVELOPER
        await engine.dispose()
    asyncio.run(_s())
