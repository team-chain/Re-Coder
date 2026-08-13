"""
감사 체인 — Codex P1(해시 전체 페이로드) · P2(분실 디바이스 sync) 회귀.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.environ.setdefault("CONTROL_PLANE_DATABASE_URL", "sqlite+aiosqlite://")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from control_plane.db.models import (  # noqa: E402
    AuditEvent, Base, Device, DeviceStatus, Organization, OrgMember, User,
)
from control_plane.models.schemas import (  # noqa: E402
    AuditAction, AuditEventCreate, OrgRole,
)


async def _fresh():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _mk_event(**kw):
    base = dict(
        action=AuditAction.DEPLOYMENT_APPROVED,
        resource_type="deployment", resource_id="d1",
        before_state={"decision": "deny"},
        after_state={"decision": "allow"},
        ip_address="10.0.0.1",
        occurred_at=datetime.now(timezone.utc),
        policy_bundle_version="v1.0.0",
        extra={})
    base.update(kw)
    return AuditEventCreate(**base)


def test_tampering_any_persisted_field_breaks_the_chain():
    """[Codex P1 회귀] before/after_state·ip·policy_version·is_suspicious 를
    DB 에서 고치면 verify_chain 이 위조를 잡아야 한다 — 예전 해시는 이
    필드들을 덮지 않아 전부 유효 판정이었다."""
    from sqlalchemy import select
    from control_plane.services.audit import AuditService

    tampers = [
        ("after_state", {"decision": "deny"}),
        ("before_state", {"decision": "allow"}),
        ("ip_address", "6.6.6.6"),
        ("policy_bundle_version", "v9.9.9"),
        ("is_suspicious", True),
    ]

    async def _scenario():
        for field, forged in tampers:
            engine, Session = await _fresh()
            async with Session() as db:
                org = Organization(name="o", slug=f"a-{field}")
                u = User(email=f"{field}@x.io", display_name="u", is_active=True,
                         oidc_provider="github", oidc_subject=f"a{field}")
                db.add_all([org, u]); await db.flush()
                svc = AuditService(db)
                await svc.record(org_id=org.org_id, actor_user_id=u.user_id,
                                 event=_mk_event())
                ok, err = await svc.verify_chain(org.org_id)
                assert ok, f"정상 체인이 위조 판정: {err}"

                # ORM setattr 는 만료 속성 로드를 유발하므로 core UPDATE 로 변조
                from sqlalchemy import update as _update
                await db.execute(_update(AuditEvent).values(**{field: forged}))
                ok, _ = await svc.verify_chain(org.org_id)
                assert not ok, f"{field} 변조가 잡히지 않았다 — 해시가 이 필드를 덮지 않는다"
            await engine.dispose()

    asyncio.run(_scenario())


def test_lost_device_can_sync_and_events_are_marked_suspicious():
    """[Codex P2 회귀] LOST 디바이스가 sync **전용** 경로로는 인증되고,
    큐 이벤트가 suspicious 로 기록된다. 일반 검증은 여전히 거부한다."""
    from sqlalchemy import select
    from control_plane.services.identity import IdentityService
    from control_plane.api.middleware.device_auth import DeviceContext
    from control_plane.api.routes.audit import sync_pending_events
    from control_plane.models.schemas import (
        AuditSyncRequest, DeviceEnrollRequest,
    )

    # 라우트가 실제로 sync 전용 의존성에 결선돼 있는지 — 일반 인증으로
    # 되돌리면 LOST 는 이 핸들러에 도달조차 못 한다.
    import inspect as _inspect
    from control_plane.api.middleware.device_auth import get_audit_sync_device
    sig = _inspect.signature(sync_pending_events)
    assert sig.parameters["ctx"].default.dependency is get_audit_sync_device, (
        "sync 라우트가 일반 인증(get_current_device)에 묶여 있다"
    )

    async def _scenario():
        engine, Session = await _fresh()
        async with Session() as db:
            org = Organization(name="o", slug="lost1")
            u = User(email="l@x.io", display_name="u", is_active=True,
                     oidc_provider="github", oidc_subject="l1")
            db.add_all([org, u]); await db.flush()
            db.add(OrgMember(org_id=org.org_id, user_id=u.user_id,
                             role=OrgRole.DEVELOPER)); await db.flush()

            svc = IdentityService(db)
            tok = await svc.enroll_device(
                user_id=u.user_id, org_id=org.org_id, role=OrgRole.DEVELOPER,
                request=DeviceEnrollRequest(display_name="d", os_type="linux",
                                            vscode_version="1", extension_version="1"))

            dev = (await db.execute(select(Device))).scalars().first()
            dev.status = DeviceStatus.LOST
            await db.flush()

            # 일반 검증은 LOST 를 거부 — 완화는 sync 전용 경로에만 있다.
            assert await svc.validate_token(tok.token) is None
            lost_dev = await svc.validate_token_for_audit_sync(tok.token)
            assert lost_dev is not None, "sync 전용 검증이 LOST 를 통과시키지 못했다"

            ctx = DeviceContext(device=lost_dev, user_id=u.user_id,
                                org_id=org.org_id, role=OrgRole.DEVELOPER)
            req = AuditSyncRequest(
                device_id=lost_dev.device_id,
                events=[_mk_event()])
            out = await sync_pending_events(org.org_id, req, ctx, db)
            assert out.accepted == 1, f"분실 디바이스 큐가 거부됐다: {out}"

            row = (await db.execute(select(AuditEvent))).scalars().first()
            assert row.is_suspicious is True, "분실 디바이스 이벤트가 suspicious 로 표시되지 않았다"
        await engine.dispose()

    asyncio.run(_scenario())
