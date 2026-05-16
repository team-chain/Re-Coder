"""
ReCoder Control Plane — SQLAlchemy ORM Models

PostgreSQL 기반. Row Level Security (RLS)는 migration에서 별도 적용.
UPDATE / DELETE는 DB 레벨 트리거로 금지 (AuditLog 보호).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Enum as SAEnum,
    ForeignKey, Index, Integer, String, Text, UniqueConstraint,
    JSON, BigInteger,
)
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.dialects.postgresql import UUID

from control_plane.models.schemas import (
    OIDCProvider, DeviceStatus, OrgRole, AuditAction,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Q2-A1: Identity & Device
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    user_id = Column(String(36), primary_key=True, default=_uuid)
    email = Column(String(255), nullable=False, unique=True, index=True)
    display_name = Column(String(255), nullable=False)
    oidc_provider = Column(SAEnum(OIDCProvider), nullable=False)
    oidc_subject = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    devices = relationship("Device", back_populates="user")
    org_memberships = relationship("OrgMember", back_populates="user")

    __table_args__ = (
        UniqueConstraint("oidc_provider", "oidc_subject", name="uq_oidc_identity"),
    )


class Device(Base):
    """
    개발자 머신에 설치된 Extension 인스턴스.

    Device Lease Policy:
    - 짧은 lease (expires_at)
    - 원격 작업 전 heartbeat 필수
    - revoked → 다음 heartbeat에서 즉시 차단
    """
    __tablename__ = "devices"

    device_id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    org_id = Column(String(36), ForeignKey("organizations.org_id"), nullable=False, index=True)
    display_name = Column(String(255), nullable=False)
    os_type = Column(String(50), nullable=False)
    vscode_version = Column(String(50), nullable=True)
    extension_version = Column(String(50), nullable=True)
    token_hash = Column(String(64), nullable=False)     # SHA-256 of raw token
    status = Column(SAEnum(DeviceStatus), nullable=False, default=DeviceStatus.ACTIVE)
    enrolled_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoke_reason = Column(String(500), nullable=True)

    user = relationship("User", back_populates="devices")
    org = relationship("Organization", back_populates="devices")

    __table_args__ = (
        Index("ix_devices_org_status", "org_id", "status"),
    )


# ---------------------------------------------------------------------------
# Q2-A2: Organization & RBAC
# ---------------------------------------------------------------------------


class Organization(Base):
    __tablename__ = "organizations"

    org_id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    is_active = Column(Boolean, nullable=False, default=True)

    members = relationship("OrgMember", back_populates="org")
    workspaces = relationship("Workspace", back_populates="org")
    devices = relationship("Device", back_populates="org")
    audit_events = relationship("AuditEvent", back_populates="org")


class OrgMember(Base):
    __tablename__ = "org_members"

    member_id = Column(String(36), primary_key=True, default=_uuid)
    org_id = Column(String(36), ForeignKey("organizations.org_id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    role = Column(SAEnum(OrgRole), nullable=False, default=OrgRole.DEVELOPER)
    joined_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    invited_by_user_id = Column(String(36), ForeignKey("users.user_id"), nullable=True)

    org = relationship("Organization", back_populates="members")
    user = relationship("User", back_populates="org_memberships", foreign_keys=[user_id])

    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_org_member"),
    )


class Workspace(Base):
    __tablename__ = "workspaces"

    workspace_id = Column(String(36), primary_key=True, default=_uuid)
    org_id = Column(String(36), ForeignKey("organizations.org_id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    org = relationship("Organization", back_populates="workspaces")
    projects = relationship("Project", back_populates="workspace")


class Project(Base):
    __tablename__ = "projects"

    project_id = Column(String(36), primary_key=True, default=_uuid)
    workspace_id = Column(String(36), ForeignKey("workspaces.workspace_id"), nullable=False, index=True)
    org_id = Column(String(36), ForeignKey("organizations.org_id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    repo_url = Column(String(500), nullable=True)
    stack = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    workspace = relationship("Workspace", back_populates="projects")


# ---------------------------------------------------------------------------
# Q2-A3: AuditLog (tamper-evident hash chain)
# ---------------------------------------------------------------------------


class AuditEvent(Base):
    """
    AuditLog — tamper-evident, NOT tamper-proof.

    설계서 §Q2-A3:
    - UPDATE와 DELETE를 DB 레벨 트리거로 금지
    - org_id 단위 hash chain (monotonic sequence)
    - event_hash = SHA-256(previous_event_hash + canonical_json(event_body))
    - 장기 archive: S3 Object Lock WORM
    """
    __tablename__ = "audit_events"

    event_id = Column(String(36), primary_key=True, default=_uuid)
    org_id = Column(String(36), ForeignKey("organizations.org_id"), nullable=False, index=True)
    seq = Column(BigInteger, nullable=False)         # org_id 단위 monotonic sequence
    actor_user_id = Column(String(36), nullable=False, index=True)
    actor_device_id = Column(String(36), nullable=True)
    action = Column(SAEnum(AuditAction), nullable=False)
    resource_type = Column(String(100), nullable=False)
    resource_id = Column(String(255), nullable=True)
    before_state = Column(JSON, nullable=True)
    after_state = Column(JSON, nullable=True)
    ip_address = Column(String(45), nullable=True)    # IPv4/IPv6
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    event_hash = Column(String(64), nullable=False)
    previous_event_hash = Column(String(64), nullable=False)
    policy_bundle_version = Column(String(100), nullable=True)
    extra = Column(JSON, default=dict)
    is_suspicious = Column(Boolean, default=False)    # lost device 이후 이벤트

    org = relationship("Organization", back_populates="audit_events")

    __table_args__ = (
        UniqueConstraint("org_id", "seq", name="uq_audit_org_seq"),
        Index("ix_audit_org_occurred", "org_id", "occurred_at"),
        Index("ix_audit_actor", "actor_user_id"),
    )


class AuditSeqCounter(Base):
    """org_id 단위 monotonic sequence 관리 (hash chain 동시성 안전)"""
    __tablename__ = "audit_seq_counters"

    org_id = Column(String(36), ForeignKey("organizations.org_id"), primary_key=True)
    last_seq = Column(BigInteger, nullable=False, default=0)
    last_event_hash = Column(String(64), nullable=False, default="0" * 64)


class PendingAuditQueue(Base):
    """Local Core 오프라인 시 쌓이는 pending AuditLog (로컬 SQLite)"""
    __tablename__ = "pending_audit_queue"

    local_id = Column(String(36), primary_key=True, default=_uuid)
    device_id = Column(String(36), nullable=False, index=True)
    payload_json = Column(Text, nullable=False)     # JSON-serialized AuditEventCreate
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    retry_count = Column(Integer, nullable=False, default=0)
    is_suspicious = Column(Boolean, default=False)
