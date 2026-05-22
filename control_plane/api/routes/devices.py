"""
Control Plane — Q2-A1: Device 관리 라우트.

- POST /devices/heartbeat            — 1분마다 heartbeat (Extension이 호출)
- GET  /devices/me                   — 현재 Device 정보
- GET  /devices                      — org 내 Device 목록 (admin/owner만)
- POST /devices/{id}/revoke          — Device 폐기
- POST /devices/{id}/rotate-token    — Device Token 회전 (ADR-006 토큰 회전 요구)
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
    DeviceTokenResponse,
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


@router.post("/{device_id}/rotate-token", response_model=DeviceTokenResponse)
async def rotate_device_token(
    device_id: str,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> DeviceTokenResponse:
    """
    Device Token 회전 (ADR-006 토큰 회전 요구).

    호출자는 본인 소유의 Device 또는 device:revoke 권한을 가진 admin/owner 가
    같은 org 의 다른 Device 를 회전시킬 수 있다.

    회전 즉시 이전 토큰은 무효화되며, Extension 은 새 토큰을 OS Keychain 에
    저장해야 한다. AuditLog 에 device.rotated 가 SECURITY_ESCALATION 이 아닌
    일반 audit 으로 기록된다 (POLICY_BUNDLE_UPDATED 와 동일 분류).
    """
    # 권한: 본인 device 거나 device:revoke 권한 보유자
    from control_plane.models.schemas import has_permission
    if device_id != ctx.device_id and not has_permission(ctx.role, Permission.DEVICE_REVOKE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="rotate-token requires owning the device or device:revoke permission",
        )

    svc = IdentityService(db)
    response = await svc.rotate_device_token(device_id=device_id, org_id=ctx.org_id)
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found, not ACTIVE, or not in your organization",
        )

    # AuditLog
    from control_plane.services.audit import AuditService
    import datetime as _dt
    try:
        audit_svc = AuditService(db)
        await audit_svc.record(
            org_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            actor_device_id=ctx.device_id,
            event=AuditEventCreate(
                action=AuditAction.DEVICE_ENROLLED,  # 재발급도 enrolled 카테고리로 분류
                resource_type="device",
                resource_id=device_id,
                after_state={"rotated": True, "by_self": device_id == ctx.device_id},
                occurred_at=_dt.datetime.now(_dt.timezone.utc),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("rotate-token audit log failed: %s", exc)

    return response
