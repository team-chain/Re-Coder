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


# ===========================================================================
# Q2-B: PolicyBundle / OPA / Approval (설계서 §Q2-B)
# ===========================================================================


class PolicyPresetKey(str, Enum):
    """설계서 §Q2-B Preset Policy 5개"""
    TRIVY_CRITICAL_BLOCK = "trivy_critical_block"
    PROD_MAIN_BRANCH_ONLY = "prod_main_branch_only"
    PORT_22_BLOCK = "port_22_block"
    SECRET_ENV_ESCALATE = "secret_env_escalate"
    LEVEL3_TWO_APPROVERS = "level3_two_approvers"


class OPADecisionStatus(str, Enum):
    """OPA 평가 출력 5단계 (설계서 §Q2-B)"""
    ALLOW = "allow"
    ALLOW_WITH_APPROVAL = "allow_with_approval"
    DENY = "deny"
    DENY_WITH_FIX_SUGGESTION = "deny_with_fix_suggestion"
    ESCALATE_TO_SECURITY = "escalate_to_security"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# PolicyBundle
# ---------------------------------------------------------------------------

class PolicyPresetConfig(BaseModel):
    """Preset 하나의 on/off 설정"""
    key: PolicyPresetKey
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class PolicyBundleCreate(BaseModel):
    org_id: str
    display_name: str
    presets: list[PolicyPresetConfig]


class PolicyBundleResponse(BaseModel):
    bundle_id: str
    org_id: str
    version: str         # e.g. "v1.0.0"
    display_name: str
    sha256: str          # SHA-256 of rego content
    is_active: bool
    created_at: datetime
    presets: list[PolicyPresetConfig] = Field(default_factory=list)

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# OPA 평가
# ---------------------------------------------------------------------------

class OPAEvaluateRequest(BaseModel):
    """Local Core → Control Plane OPA 평가 요청"""
    action: str              # e.g. "deployment:request"
    resource_type: str
    resource_id: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)  # branch, image, env vars, ports…
    level: int = Field(ge=1, le=4)
    policy_bundle_version: Optional[str] = None


class OPAEvaluateResponse(BaseModel):
    """OPA 평가 결과"""
    decision: OPADecisionStatus
    reason: str
    fix_suggestion: Optional[str] = None    # deny_with_fix_suggestion일 때
    required_approvers: int = 0             # allow_with_approval일 때
    approval_request_id: Optional[str] = None
    policy_bundle_version: str
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# ApprovalRequest (Multi-Approver)
# ---------------------------------------------------------------------------

class ApprovalRequestCreate(BaseModel):
    org_id: str
    requester_user_id: str
    requester_device_id: Optional[str] = None
    action_summary: str
    resource_type: str
    resource_id: Optional[str] = None
    command_preview: Optional[str] = None     # 실행될 명령 미리보기
    risk_reason: str
    required_approvers: int = Field(default=2, ge=1)
    expires_in_hours: int = Field(default=24, ge=1, le=168)
    policy_bundle_version: str
    context: dict[str, Any] = Field(default_factory=dict)


class ApprovalVoteRequest(BaseModel):
    approved: bool
    reason: str   # 거부 시 필수, 승인 시에도 권고


class ApprovalVoteResponse(BaseModel):
    vote_id: str
    approval_request_id: str
    voter_user_id: str
    approved: bool
    reason: str
    voted_at: datetime

    model_config = {"from_attributes": True}


class ApprovalRequestResponse(BaseModel):
    approval_request_id: str
    org_id: str
    requester_user_id: str
    action_summary: str
    resource_type: str
    resource_id: Optional[str] = None
    command_preview: Optional[str] = None
    risk_reason: str
    status: ApprovalStatus
    required_approvers: int
    current_approvals: int
    current_rejections: int
    expires_at: datetime
    policy_bundle_version: str
    created_at: datetime
    votes: list[ApprovalVoteResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}
