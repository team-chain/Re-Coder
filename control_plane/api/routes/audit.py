"""
Control Plane — Q2-A3: AuditLog 라우트

- GET  /audit/{org_id}/events              — AuditLog 목록 (audit:read 필요)
- POST /audit/{org_id}/sync                — 오프라인 pending events 재전송
- GET  /audit/{org_id}/verify              — hash chain 무결성 검증 (admin+)
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.middleware.device_auth import (
    DeviceContext,
    get_current_device,
    require_permission_dep,
)
from control_plane.db.session import get_db
from control_plane.models.schemas import (
    AuditAction,
    AuditEventResponse,
    AuditSyncRequest,
    AuditSyncResponse,
    Permission,
)
from control_plane.services.audit import AuditService
from control_plane.services.org_service import OrgService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/audit", tags=["audit"])


@router.get(
    "/{org_id}/events",
    response_model=list[AuditEventResponse],
    dependencies=[Depends(require_permission_dep("audit:read"))],
)
async def list_audit_events(
    org_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    action: Optional[AuditAction] = Query(default=None),
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> list[AuditEventResponse]:
    """
    AuditLog 조회 (audit:read 권한 필요).
    멀티테넌트 격리: 자신의 org_id만 접근 가능.
    """
    if ctx.org_id != org_id:
        raise HTTPException(status_code=403, detail="Access to this organization is not permitted")

    svc = AuditService(db)
    return await svc.list_events(
        org_id=org_id,
        limit=limit,
        offset=offset,
        action_filter=action,
    )


@router.post("/{org_id}/sync", response_model=AuditSyncResponse)
async def sync_pending_events(
    org_id: str,
    request: AuditSyncRequest,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> AuditSyncResponse:
    """
    오프라인 모드 중 쌓인 pending AuditLog 재전송.

    설계서 §Q2-A3:
    - device가 lost로 표시된 경우 is_suspicious=True
    - 재전송 보장 전까지 프로덕션 작업 차단
    """
    if ctx.org_id != org_id:
        raise HTTPException(status_code=403, detail="Access to this organization is not permitted")

    # device device_id 일치 확인
    if request.device_id != ctx.device_id:
        raise HTTPException(status_code=403, detail="Device ID mismatch")

    from control_plane.db.models import Device, DeviceStatus
    from sqlalchemy import select
    result = await db.execute(
        select(Device).where(Device.device_id == ctx.device_id)
    )
    device = result.scalar_one()
    device_is_suspicious = device.status == DeviceStatus.LOST

    svc = AuditService(db)
    response = await svc.sync_pending(
        org_id=org_id,
        actor_user_id=ctx.user_id,
        request=request,
        device_is_suspicious=device_is_suspicious,
    )

    logger.info(
        "AuditLog sync: org=%s device=%s accepted=%d rejected=%d suspicious=%s",
        org_id, ctx.device_id, response.accepted, response.rejected, device_is_suspicious,
    )
    return response


@router.get("/{org_id}/verify")
async def verify_chain(
    org_id: str,
    limit: int = Query(default=1000, ge=1, le=10000),
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    hash chain 무결성 검증 (audit:export 권한 필요 = admin/owner/auditor).
    tamper-evident: 조작 흔적을 사후 탐지한다.
    """
    if ctx.org_id != org_id:
        raise HTTPException(status_code=403, detail="Access to this organization is not permitted")

    org_svc = OrgService(db)
    try:
        await org_svc.require_permission(org_id, ctx.user_id, Permission.AUDIT_EXPORT)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    svc = AuditService(db)
    valid, error_message = await svc.verify_chain(org_id=org_id, limit=limit)

    return {
        "org_id": org_id,
        "valid": valid,
        "error_message": error_message,
        "events_checked": limit,
    }
