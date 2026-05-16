"""
Control Plane — Q2-A1: Device 관리 라우트

- GET  /devices/heartbeat   — 1분마다 heartbeat (Extension이 호출)
- GET  /devices/me          — 현재 Device 정보
- GET  /devices             — org 내 Device 목록 (admin/owner만)
- POST /devices/{id}/revoke — Device 폐기
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.middleware.device_auth import (
    DeviceContext,
    get_current_device,
    require_permission_dep,
)
from control_plane.db.models import Device
from control_plane.db.session import get_db
from control_plane.models.schemas import (
    AuditAction,
    AuditEventCreate,
    DeviceHeartbeatRequest,
    DeviceHeartbeatResponse,
    DeviceStatus,
    Permission,
)
from control_plane.services.identity import IdentityService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/devices", tags=["devices"])


@router.post("/heartbeat", response_model=DeviceHeartbeatResponse)
async def heartbeat(
    request: DeviceHeartbeatRequest,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> DeviceHeartbeatResponse:
    """
    Extension이 1분마다 호출하는 heartbeat.
    revoked / expired이면 Extension이 즉시 작업을 차단해야 한다.
    pending_audit_count가 있으면 /audit/sync 호출을 트리거한다.
    """
    from control_plane.api.middleware.device_auth import _extract_token  # raw_token 접근 우회
    # heartbeat은 미들웨어에서 이미 validate_token을 통과했으므로 device가 ACTIVE
    svc = IdentityService(db)
    # last_heartbeat_at 갱신
    ctx.device.last_heartbeat_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )
    await db.flush()

    # 최신 policy bundle version (Q2-B에서 구현)
    policy_version: Optional[str] = None

    response = DeviceHeartbeatResponse(
        status="ok",
        device_status=DeviceStatus.ACTIVE,
        policy_bundle_version=policy_version,
    )

    if request.pending_audit_count > 0:
        logger.info(
            "Device %s has %d pending audit events — client should sync",
            ctx.device_id, request.pending_audit_count,
        )

    return response


@router.get("/me")
async def get_my_device(ctx: DeviceContext = Depends(get_current_device)) -> dict:
    """현재 인증된 Device 정보 반환"""
    d = ctx.device
    return {
        "device_id": d.device_id,
        "display_name": d.display_name,
        "os_type": d.os_type,
        "status": d.status.value,
        "expires_at": d.expires_at.isoformat(),
        "last_heartbeat_at": d.last_heartbeat_at.isoformat() if d.last_heartbeat_at else None,
        "org_id": d.org_id,
        "user_id": d.user_id,
        "role": ctx.role.value,
    }


@router.get("", dependencies=[Depends(require_permission_dep("device:revoke"))])
async def list_devices(
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """org 내 전체 Device 목록 (device:revoke 권한 필요 = admin/owner)"""
    result = await db.execute(
        select(Device).where(Device.org_id == ctx.org_id)
    )
    devices = result.scalars().all()
    return [
        {
            "device_id": d.device_id,
            "user_id": d.user_id,
            "display_name": d.display_name,
            "os_type": d.os_type,
            "status": d.status.value,
            "enrolled_at": d.enrolled_at.isoformat(),
            "expires_at": d.expires_at.isoformat(),
            "last_heartbeat_at": d.last_heartbeat_at.isoformat() if d.last_heartbeat_at else None,
        }
        for d in devices
    ]


@router.post("/{device_id}/revoke", dependencies=[Depends(require_permission_dep("device:revoke"))])
async def revoke_device(
    device_id: str,
    reason: str,
    mark_lost: bool = False,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Device 즉시 폐기.
    다음 heartbeat에서 Extension이 차단된다.
    AuditLog에 기록한다.
    """
    # 대상 Device가 같은 org인지 확인
    result = await db.execute(
        select(Device).where(Device.device_id == device_id, Device.org_id == ctx.org_id)
    )
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="Device not found in this organization")

    svc = IdentityService(db)
    ok = await svc.revoke_device(device_id, reason=reason, mark_lost=mark_lost)
    if not ok:
        raise HTTPException(status_code=404, detail="Device not found")

    # AuditLog 기록
    from control_plane.services.audit import AuditService
    import datetime as _dt
    audit_svc = AuditService(db)
    await audit_svc.record(
        org_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        event=AuditEventCreate(
            action=AuditAction.DEVICE_REVOKED,
            resource_type="device",
            resource_id=device_id,
            after_state={"reason": reason, "mark_lost": mark_lost},
            occurred_at=_dt.datetime.now(_dt.timezone.utc),
        ),
        actor_device_id=ctx.device_id,
    )

    return {"status": "revoked", "device_id": device_id}
