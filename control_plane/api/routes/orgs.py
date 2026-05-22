"""
Control Plane — Q2-A2: Organization & RBAC 라우트

- POST /orgs                    — 조직 생성
- GET  /orgs/{org_id}           — 조직 정보
- GET  /orgs/{org_id}/members   — 멤버 목록
- POST /orgs/{org_id}/members   — 멤버 초대
- DELETE /orgs/{org_id}/members/{user_id} — 멤버 제거
- PUT  /orgs/{org_id}/members/{user_id}/role — 역할 변경
- POST /orgs/{org_id}/workspaces — 워크스페이스 생성
- GET  /orgs/{org_id}/workspaces — 워크스페이스 목록
- POST /orgs/{org_id}/projects  — 프로젝트 생성
- GET  /orgs/{org_id}/projects  — 프로젝트 목록
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.middleware.device_auth import (
    DeviceContext,
    get_current_device,
)
from control_plane.db.session import get_db
from control_plane.models.schemas import (
    AuditAction,
    AuditEventCreate,
    OrgMemberInvite,
    OrgMemberResponse,
    OrganizationCreate,
    OrganizationResponse,
    Permission,
    ProjectCreate,
    ProjectResponse,
    RoleChangeRequest,
    WorkspaceCreate,
    WorkspaceResponse,
)
from control_plane.services.org_service import OrgService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orgs", tags=["organizations"])


# ---------------------------------------------------------------------------
# Organization
# ---------------------------------------------------------------------------

@router.post("", response_model=OrganizationResponse, status_code=status.HTTP_201_CREATED)
async def create_org(
    request: OrganizationCreate,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> OrganizationResponse:
    """조직 생성. 생성자가 자동으로 owner로 등록된다."""
    svc = OrgService(db)
    try:
        return await svc.create_org(request, creator_user_id=ctx.user_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/{org_id}", response_model=OrganizationResponse)
async def get_org(
    org_id: str,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> OrganizationResponse:
    _assert_same_org(ctx, org_id)
    svc = OrgService(db)
    org = await svc.get_org(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


# ---------------------------------------------------------------------------
# Members & RBAC
# ---------------------------------------------------------------------------

@router.get("/{org_id}/members", response_model=list[OrgMemberResponse])
async def list_members(
    org_id: str,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> list[OrgMemberResponse]:
    _assert_same_org(ctx, org_id)
    svc = OrgService(db)
    await svc.require_permission(org_id, ctx.user_id, Permission.PROJECT_READ)
    return await svc.list_members(org_id)


@router.post("/{org_id}/members", response_model=OrgMemberResponse, status_code=status.HTTP_201_CREATED)
async def invite_member(
    org_id: str,
    invite: OrgMemberInvite,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> OrgMemberResponse:
    _assert_same_org(ctx, org_id)
    svc = OrgService(db)
    try:
        member = await svc.add_member(org_id, inviter_user_id=ctx.user_id, invite=invite)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # AuditLog
    await _audit(db, ctx, org_id, AuditAction.ORG_MEMBER_ADDED, "org_member", member.user_id,
                 after_state={"email": invite.email, "role": invite.role.value})
    return member


@router.delete("/{org_id}/members/{target_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    org_id: str,
    target_user_id: str,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> None:
    _assert_same_org(ctx, org_id)
    svc = OrgService(db)
    try:
        ok = await svc.remove_member(org_id, requester_user_id=ctx.user_id, target_user_id=target_user_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not ok:
        raise HTTPException(status_code=404, detail="Member not found")
    await _audit(db, ctx, org_id, AuditAction.ORG_MEMBER_REMOVED, "org_member", target_user_id)


@router.put("/{org_id}/members/{target_user_id}/role", response_model=OrgMemberResponse)
async def change_role(
    org_id: str,
    target_user_id: str,
    body: RoleChangeRequest,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> OrgMemberResponse:
    _assert_same_org(ctx, org_id)
    body.user_id = target_user_id  # URL param 우선
    svc = OrgService(db)
    try:
        member = await svc.change_role(org_id, requester_user_id=ctx.user_id, request=body)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(db, ctx, org_id, AuditAction.ORG_ROLE_CHANGED, "org_member", target_user_id,
                 after_state={"new_role": body.new_role.value, "reason": body.reason})
    return member


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

@router.post("/{org_id}/workspaces", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    org_id: str,
    request: WorkspaceCreate,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceResponse:
    _assert_same_org(ctx, org_id)
    request.org_id = org_id
    svc = OrgService(db)
    try:
        return await svc.create_workspace(requester_user_id=ctx.user_id, request=request)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/{org_id}/workspaces", response_model=list[WorkspaceResponse])
async def list_workspaces(
    org_id: str,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> list[WorkspaceResponse]:
    _assert_same_org(ctx, org_id)
    svc = OrgService(db)
    try:
        await svc.require_permission(org_id, ctx.user_id, Permission.PROJECT_READ)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return await svc.list_workspaces(org_id)


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

@router.post("/{org_id}/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    org_id: str,
    request: ProjectCreate,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    _assert_same_org(ctx, org_id)
    svc = OrgService(db)
    try:
        return await svc.create_project(requester_user_id=ctx.user_id, request=request)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{org_id}/projects", response_model=list[ProjectResponse])
async def list_projects(
    org_id: str,
    workspace_id: Optional[str] = None,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectResponse]:
    _assert_same_org(ctx, org_id)
    svc = OrgService(db)
    try:
        await svc.require_permission(org_id, ctx.user_id, Permission.PROJECT_READ)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return await svc.list_projects(org_id, workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_same_org(ctx: DeviceContext, org_id: str) -> None:
    """멀티테넌트 격리: 자신의 org_id만 접근 가능"""
    if ctx.org_id != org_id:
        raise HTTPException(status_code=403, detail="Access to this organization is not permitted")


async def _audit(
    db: AsyncSession,
    ctx: DeviceContext,
    org_id: str,
    action: AuditAction,
    resource_type: str,
    resource_id: Optional[str] = None,
    before_state: Optional[dict] = None,
    after_state: Optional[dict] = None,
) -> None:
    import datetime as _dt
    from control_plane.services.audit import AuditService
    audit_svc = AuditService(db)
    await audit_svc.record(
        org_id=org_id,
        actor_user_id=ctx.user_id,
        event=AuditEventCreate(
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            before_state=before_state,
            after_state=after_state,
            occurred_at=_dt.datetime.now(_dt.timezone.utc),
        ),
        actor_device_id=ctx.device_id,
    )
