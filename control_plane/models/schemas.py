"""
ReCoder Control Plane — Q2-A Data Schemas (Pydantic v2)

Q2-A1: Identity & Device
Q2-A2: Organization & RBAC
Q2-A3: AuditLog & Sync
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, EmailStr


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class OIDCProvider(str, Enum):
    GOOGLE = "google"
    GITHUB = "github"


class DeviceStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    LOST = "lost"
    EXPIRED = "expired"


class OrgRole(str, Enum):
    """RBAC 역할 (설계서 §Q2-A2)"""
    OWNER = "owner"          # 모든 권한
    ADMIN = "admin"          # 조직과 정책 관리
    DEVELOPER = "developer"  # 배포 요청 가능, 승인 불가
    APPROVER = "approver"    # 배포 승인 가능
    AUDITOR = "auditor"      # 감사 로그 조회만
    VIEWER = "viewer"        # 읽기 전용


class Permission(str, Enum):
    """RBAC 권한 도메인 (설계서 §Q2-A2)"""
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    DEVICE_ENROLL = "device:enroll"
    DEVICE_REVOKE = "device:revoke"
    DEPLOYMENT_REQUEST = "deployment:request"
    DEPLOYMENT_APPROVE = "deployment:approve"
    DEPLOYMENT_OVERRIDE = "deployment:override"
    POLICY_READ = "policy:read"
    POLICY_WRITE = "policy:write"
    POLICY_ASSIGN = "policy:assign"
    AUDIT_READ = "audit:read"
    AUDIT_EXPORT = "audit:export"
    SECRET_UPDATE = "secret:update"
    PRODUCTION_DEPLOY = "production:deploy"
    BREAKGLASS_EXECUTE = "breakglass:execute"


class OfflineLevel(str, Enum):
    """오프라인 허용 작업 레벨 (설계서 §Device Lease Policy)"""
    LEVEL_1_2 = "level_1_2"   # 항상 허용
    LEVEL_3 = "level_3"       # 정책 캐시 유효 + heartbeat 1시간 이내
    LEVEL_4 = "level_4"       # 항상 차단
    PRODUCTION = "production"  # 항상 차단


class AuditAction(str, Enum):
    """AuditLog 액션 타입"""
    # Identity
    USER_LOGIN = "user.login"
    USER_LOGOUT = "user.logout"
    DEVICE_ENROLLED = "device.enrolled"
    DEVICE_REVOKED = "device.revoked"
    DEVICE_HEARTBEAT = "device.heartbeat"
    # Organization
    ORG_CREATED = "org.created"
    ORG_MEMBER_ADDED = "org.member_added"
    ORG_MEMBER_REMOVED = "org.member_removed"
    ORG_ROLE_CHANGED = "org.role_changed"
    # Deployment
    DEPLOYMENT_REQUESTED = "deployment.requested"
    DEPLOYMENT_APPROVED = "deployment.approved"
    DEPLOYMENT_REJECTED = "deployment.rejected"
    DEPLOYMENT_EXECUTED = "deployment.executed"
    DEPLOYMENT_ROLLBACK = "deployment.rollback"
    ROLLBACK_PR_CREATED = "deployment.rollback_pr_created"
    # Policy
    POLICY_BUNDLE_UPDATED = "policy.bundle_updated"
    POLICY_EVALUATED = "policy.evaluated"
    # Security
    UPLOAD_REJECTED = "upload.rejected"
    SUSPICIOUS_ACTIVITY = "security.suspicious_activity"


# ---------------------------------------------------------------------------
# Q2-A1: Identity & Device
# ---------------------------------------------------------------------------


class UserBase(BaseModel):
    email: str
    display_name: str
    oidc_provider: OIDCProvider
    oidc_subject: str   # provider's unique user ID


class UserCreate(UserBase):
    pass


class UserResponse(UserBase):
    user_id: str
    created_at: datetime
    last_login_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class DeviceEnrollRequest(BaseModel):
    """Extension이 Control Plane에 전송하는 Device 등록 요청"""
    display_name: str           # e.g., "MacBook Pro (이동규)"
    os_type: str                # "windows" | "macos" | "linux"
    vscode_version: str
    extension_version: str
    public_key_pem: Optional[str] = None  # 향후 mTLS 대비


class DeviceTokenResponse(BaseModel):
    """Control Plane이 발급하는 Device Token"""
    device_id: str
    token: str                  # OS Keychain에 저장할 값
    expires_at: datetime
    org_id: str
    user_id: str
    role: OrgRole


class DeviceHeartbeatRequest(BaseModel):
    device_id: str
    local_core_version: Optional[str] = None
    pending_audit_count: int = 0   # 로컬에 쌓인 pending AuditLog 수


class DeviceHeartbeatResponse(BaseModel):
    status: str                 # "ok" | "revoked" | "expired"
    device_status: DeviceStatus
    policy_bundle_version: Optional[str] = None   # 최신 정책 버전 (업데이트 필요 시)
    server_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OIDCCallbackRequest(BaseModel):
    provider: OIDCProvider
    code: str           # Authorization code
    redirect_uri: str
    state: str          # CSRF 방지


class OIDCTokenResponse(BaseModel):
    """OIDC 인증 완료 후 Extension으로 반환하는 임시 토큰 (Device 등록 전 단계)"""
    temp_token: str
    expires_in: int     # seconds
    user_id: str
    email: str
    display_name: str


# ---------------------------------------------------------------------------
# Q2-A2: Organization & Project
# ---------------------------------------------------------------------------


class OrganizationCreate(BaseModel):
    name: str
    slug: str           # URL-safe identifier


class OrganizationResponse(BaseModel):
    org_id: str
    name: str
    slug: str
    created_at: datetime
    member_count: int = 0

    model_config = {"from_attributes": True}


class WorkspaceCreate(BaseModel):
    org_id: str
    name: str
    description: str = ""


class WorkspaceResponse(BaseModel):
    workspace_id: str
    org_id: str
    name: str
    description: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ProjectCreate(BaseModel):
    workspace_id: str
    name: str
    repo_url: Optional[str] = None
    stack: Optional[str] = None


class ProjectResponse(BaseModel):
    project_id: str
    workspace_id: str
    org_id: str
    name: str
    repo_url: Optional[str] = None
    stack: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class OrgMemberInvite(BaseModel):
    email: str
    role: OrgRole


class OrgMemberResponse(BaseModel):
    user_id: str
    org_id: str
    email: str
    display_name: str
    role: OrgRole
    joined_at: datetime

    model_config = {"from_attributes": True}


class RoleChangeRequest(BaseModel):
    user_id: str
    new_role: OrgRole
    reason: str


# ---------------------------------------------------------------------------
# Q2-A3: AuditLog & Sync
# ---------------------------------------------------------------------------


class AuditEventCreate(BaseModel):
    """Local Core → Control Plane 전송 payload"""
    action: AuditAction
    resource_type: str
    resource_id: Optional[str] = None
    before_state: Optional[dict[str, Any]] = None
    after_state: Optional[dict[str, Any]] = None
    ip_address: Optional[str] = None
    occurred_at: datetime
    policy_bundle_version: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class AuditEventResponse(BaseModel):
    """Control Plane이 반환하는 AuditLog 항목"""
    event_id: str
    org_id: str
    actor_user_id: str
    actor_device_id: Optional[str] = None
    action: AuditAction
    resource_type: str
    resource_id: Optional[str] = None
    occurred_at: datetime
    event_hash: str             # 무결성 검증용
    previous_event_hash: str
    policy_bundle_version: Optional[str] = None

    model_config = {"from_attributes": True}


class AuditSyncRequest(BaseModel):
    """오프라인 중 쌓인 pending AuditLog 재전송"""
    device_id: str
    events: list[AuditEventCreate]


class AuditSyncResponse(BaseModel):
    accepted: int
    rejected: int
    rejected_reasons: list[str] = Field(default_factory=list)


class PendingAuditEvent(BaseModel):
    """Local Core 로컬 큐에 저장되는 pending event"""
    local_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event: AuditEventCreate
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    retry_count: int = 0
    is_suspicious: bool = False  # lost device 신고 후 발생한 이벤트


# ---------------------------------------------------------------------------
# RBAC 권한 매핑
# ---------------------------------------------------------------------------

ROLE_PERMISSIONS: dict[OrgRole, set[Permission]] = {
    OrgRole.OWNER: set(Permission),  # 모든 권한
    OrgRole.ADMIN: {
        Permission.PROJECT_READ, Permission.PROJECT_WRITE,
        Permission.DEVICE_ENROLL, Permission.DEVICE_REVOKE,
        Permission.DEPLOYMENT_REQUEST, Permission.DEPLOYMENT_APPROVE,
        Permission.POLICY_READ, Permission.POLICY_WRITE, Permission.POLICY_ASSIGN,
        Permission.AUDIT_READ, Permission.AUDIT_EXPORT,
        Permission.SECRET_UPDATE,
    },
    OrgRole.DEVELOPER: {
        Permission.PROJECT_READ, Permission.PROJECT_WRITE,
        Permission.DEVICE_ENROLL,
        Permission.DEPLOYMENT_REQUEST,
        Permission.POLICY_READ,
        Permission.AUDIT_READ,
    },
    OrgRole.APPROVER: {
        Permission.PROJECT_READ,
        Permission.DEPLOYMENT_REQUEST, Permission.DEPLOYMENT_APPROVE,
        Permission.POLICY_READ,
        Permission.AUDIT_READ,
    },
    OrgRole.AUDITOR: {
        Permission.PROJECT_READ,
        Permission.POLICY_READ,
        Permission.AUDIT_READ, Permission.AUDIT_EXPORT,
    },
    OrgRole.VIEWER: {
        Permission.PROJECT_READ,
        Permission.POLICY_READ,
    },
}


def has_permission(role: OrgRole, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())
