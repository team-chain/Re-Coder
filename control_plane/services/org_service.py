"""
Control Plane — Q2-A2: Organization & RBAC Service

설계서 §Q2-A2:
- Organization / Workspace / Project CRUD
- RBAC: 역할 기반 권한 제어
- 멀티테넌트 org_id 격리 (모든 쿼리에 자동 적용)
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.db.models import OrgMember, Organization, Project, User, Workspace
from control_plane.models.schemas import (
    OrgMemberInvite,
    OrgMemberResponse,
    OrgRole,
    OrganizationCreate,
    OrganizationResponse,
    Permission,
    ProjectCreate,
    ProjectResponse,
    ROLE_PERMISSIONS,
    RoleChangeRequest,
    WorkspaceCreate,
    WorkspaceResponse,
    has_permission,
)

logger = logging.getLogger(__name__)


class OrgService:
    """Organization / Workspace / Project / RBAC 관리"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Organization
    # ------------------------------------------------------------------

    async def create_org(
        self, request: OrganizationCreate, creator_user_id: str
    ) -> OrganizationResponse:
        """조직 생성 + 생성자를 owner로 등록"""
        # slug 중복 확인
        existing = await self._db.execute(
            select(Organization).where(Organization.slug == request.slug)
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError(f"Slug '{request.slug}' already exists")

        org = Organization(name=request.name, slug=request.slug)
        self._db.add(org)
        await self._db.flush()

        # 생성자를 owner로 자동 등록
        member = OrgMember(
            org_id=org.org_id,
            user_id=creator_user_id,
            role=OrgRole.OWNER,
            invited_by_user_id=creator_user_id,
        )
        self._db.add(member)
        await self._db.flush()

        logger.info("Organization created: %s (slug=%s) by user=%s", org.org_id, org.slug, creator_user_id)
        return OrganizationResponse(
            org_id=org.org_id,
            name=org.name,
            slug=org.slug,
            created_at=org.created_at,
            member_count=1,
        )

    async def get_org(self, org_id: str) -> Optional[OrganizationResponse]:
        result = await self._db.execute(
            select(Organization).where(Organization.org_id == org_id, Organization.is_active == True)
        )
        org = result.scalar_one_or_none()
        if org is None:
            return None
        count_result = await self._db.execute(
            select(OrgMember).where(OrgMember.org_id == org_id)
        )
        member_count = len(count_result.scalars().all())
        return OrganizationResponse(
            org_id=org.org_id,
            name=org.name,
            slug=org.slug,
            created_at=org.created_at,
            member_count=member_count,
        )

    # ------------------------------------------------------------------
    # Members & RBAC
    # ------------------------------------------------------------------

    async def get_member_role(self, org_id: str, user_id: str) -> Optional[OrgRole]:
        """user의 org 내 역할 조회 (없으면 None)"""
        result = await self._db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        return member.role if member else None

    async def require_permission(
        self, org_id: str, user_id: str, permission: Permission
    ) -> None:
        """
        권한 확인. 없으면 PermissionError 발생.
        모든 API 엔드포인트에서 호출한다.
        """
        role = await self.get_member_role(org_id, user_id)
        if role is None:
            raise PermissionError(f"User {user_id} is not a member of org {org_id}")
        if not has_permission(role, permission):
            raise PermissionError(
                f"Role '{role.value}' does not have permission '{permission.value}'"
            )

    async def add_member(
        self,
        org_id: str,
        inviter_user_id: str,
        invite: OrgMemberInvite,
    ) -> OrgMemberResponse:
        """멤버 초대 (이메일 기준, 이미 가입된 사용자만)"""
        # 초대자 권한 확인
        await self.require_permission(org_id, inviter_user_id, Permission.DEVICE_ENROLL)

        # 초대 대상 user 조회
        result = await self._db.execute(
            select(User).where(User.email == invite.email, User.is_active == True)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise ValueError(f"User with email '{invite.email}' not found")

        # 이미 멤버인지 확인
        existing = await self._db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == user.user_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError(f"User '{invite.email}' is already a member")

        member = OrgMember(
            org_id=org_id,
            user_id=user.user_id,
            role=invite.role,
            invited_by_user_id=inviter_user_id,
        )
        self._db.add(member)
        await self._db.flush()

        logger.info(
            "Member added: user=%s org=%s role=%s by=%s",
            user.user_id, org_id, invite.role.value, inviter_user_id,
        )
        return OrgMemberResponse(
            user_id=user.user_id,
            org_id=org_id,
            email=user.email,
            display_name=user.display_name,
            role=invite.role,
            joined_at=member.joined_at,
        )

    async def remove_member(
        self, org_id: str, requester_user_id: str, target_user_id: str
    ) -> bool:
        """멤버 제거. owner는 제거 불가 (최소 1명의 owner 보장)."""
        await self.require_permission(org_id, requester_user_id, Permission.DEVICE_REVOKE)

        result = await self._db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == target_user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            return False

        if member.role == OrgRole.OWNER:
            raise ValueError("Cannot remove the owner. Transfer ownership first.")

        await self._db.delete(member)
        await self._db.flush()
        logger.info("Member removed: user=%s from org=%s", target_user_id, org_id)
        return True

    async def change_role(
        self,
        org_id: str,
        requester_user_id: str,
        request: RoleChangeRequest,
    ) -> OrgMemberResponse:
        """역할 변경. 최소 1명의 owner 보장."""
        await self.require_permission(org_id, requester_user_id, Permission.POLICY_ASSIGN)

        result = await self._db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == request.user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise ValueError(f"User {request.user_id} is not a member of org {org_id}")

        # owner → non-owner 변경 시 다른 owner 있는지 확인
        if member.role == OrgRole.OWNER and request.new_role != OrgRole.OWNER:
            owner_count_result = await self._db.execute(
                select(OrgMember).where(
                    OrgMember.org_id == org_id,
                    OrgMember.role == OrgRole.OWNER,
                )
            )
            owners = owner_count_result.scalars().all()
            if len(owners) <= 1:
                raise ValueError("Cannot demote the last owner")

        old_role = member.role
        member.role = request.new_role
        await self._db.flush()

        user_result = await self._db.execute(select(User).where(User.user_id == request.user_id))
        user = user_result.scalar_one()

        logger.info(
            "Role changed: user=%s org=%s %s→%s (reason: %s)",
            request.user_id, org_id, old_role.value, request.new_role.value, request.reason,
        )
        return OrgMemberResponse(
            user_id=user.user_id,
            org_id=org_id,
            email=user.email,
            display_name=user.display_name,
            role=request.new_role,
            joined_at=member.joined_at,
        )

    async def list_members(self, org_id: str) -> list[OrgMemberResponse]:
        result = await self._db.execute(
            select(OrgMember).where(OrgMember.org_id == org_id)
        )
        members = result.scalars().all()
        responses = []
        for m in members:
            user_result = await self._db.execute(select(User).where(User.user_id == m.user_id))
            user = user_result.scalar_one_or_none()
            if user:
                responses.append(OrgMemberResponse(
                    user_id=user.user_id,
                    org_id=org_id,
                    email=user.email,
                    display_name=user.display_name,
                    role=m.role,
                    joined_at=m.joined_at,
                ))
        return responses

    # ------------------------------------------------------------------
    # Workspace
    # ------------------------------------------------------------------

    async def create_workspace(
        self, requester_user_id: str, request: WorkspaceCreate
    ) -> WorkspaceResponse:
        await self.require_permission(request.org_id, requester_user_id, Permission.PROJECT_WRITE)
        ws = Workspace(
            org_id=request.org_id,
            name=request.name,
            description=request.description,
        )
        self._db.add(ws)
        await self._db.flush()
        return WorkspaceResponse(
            workspace_id=ws.workspace_id,
            org_id=ws.org_id,
            name=ws.name,
            description=ws.description,
            created_at=ws.created_at,
        )

    async def list_workspaces(self, org_id: str) -> list[WorkspaceResponse]:
        result = await self._db.execute(
            select(Workspace).where(Workspace.org_id == org_id)
        )
        return [
            WorkspaceResponse(
                workspace_id=w.workspace_id,
                org_id=w.org_id,
                name=w.name,
                description=w.description or "",
                created_at=w.created_at,
            )
            for w in result.scalars().all()
        ]

    # ------------------------------------------------------------------
    # Project
    # ------------------------------------------------------------------

    async def create_project(
        self, requester_user_id: str, request: ProjectCreate
    ) -> ProjectResponse:
        # workspace 조회해서 org_id 확인
        ws_result = await self._db.execute(
            select(Workspace).where(Workspace.workspace_id == request.workspace_id)
        )
        ws = ws_result.scalar_one_or_none()
        if ws is None:
            raise ValueError(f"Workspace {request.workspace_id} not found")

        await self.require_permission(ws.org_id, requester_user_id, Permission.PROJECT_WRITE)

        project = Project(
            workspace_id=request.workspace_id,
            org_id=ws.org_id,
            name=request.name,
            repo_url=request.repo_url,
            stack=request.stack,
        )
        self._db.add(project)
        await self._db.flush()
        return ProjectResponse(
            project_id=project.project_id,
            workspace_id=project.workspace_id,
            org_id=project.org_id,
            name=project.name,
            repo_url=project.repo_url,
            stack=project.stack,
            created_at=project.created_at,
        )

    async def list_projects(self, org_id: str, workspace_id: Optional[str] = None) -> list[ProjectResponse]:
        stmt = select(Project).where(Project.org_id == org_id)
        if workspace_id:
            stmt = stmt.where(Project.workspace_id == workspace_id)
        result = await self._db.execute(stmt)
        return [
            ProjectResponse(
                project_id=p.project_id,
                workspace_id=p.workspace_id,
                org_id=p.org_id,
                name=p.name,
                repo_url=p.repo_url,
                stack=p.stack,
                created_at=p.created_at,
            )
            for p in result.scalars().all()
        ]
