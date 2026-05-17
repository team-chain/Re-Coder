"""
Control Plane — Q2-A1: Identity & Device Service

ADR-006:
- Google/GitHub OIDC만 지원
- 비밀번호 기반 자체 인증 구현하지 않음
- 타임박스: D+14 체크포인트, D+21 무조건 BaaS 피봇
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.db.models import Device, User
from control_plane.models.schemas import (
    DeviceEnrollRequest,
    DeviceHeartbeatRequest,
    DeviceHeartbeatResponse,
    DeviceStatus,
    DeviceTokenResponse,
    OIDCProvider,
    OrgRole,
    UserResponse,
)

logger = logging.getLogger(__name__)

# Device Token 유효 기간 (설계서: 짧은 lease)
_DEVICE_TOKEN_TTL_HOURS = int(os.environ.get("DEVICE_TOKEN_TTL_HOURS", "24"))

# Heartbeat 허용 오프라인 기간 (staging/dev Level 3)
_HEARTBEAT_OFFLINE_LIMIT_HOURS = 1


class IdentityService:
    """OIDC 인증 + Device 등록/관리 서비스"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # OIDC User
    # ------------------------------------------------------------------

    async def get_or_create_user(
        self,
        email: str,
        display_name: str,
        provider: OIDCProvider,
        oidc_subject: str,
    ) -> User:
        """OIDC 콜백 후 User 조회/생성 + last_login_at 갱신"""
        result = await self._db.execute(
            select(User).where(
                User.oidc_provider == provider,
                User.oidc_subject == oidc_subject,
            )
        )
        user = result.scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if user is None:
            user = User(
                email=email,
                display_name=display_name,
                oidc_provider=provider,
                oidc_subject=oidc_subject,
                last_login_at=now,
            )
            self._db.add(user)
            await self._db.flush()
            logger.info("New user created: %s (%s)", email, provider.value)
        else:
            user.last_login_at = now
            await self._db.flush()

        return user

    # ------------------------------------------------------------------
    # Device Enrollment
    # ------------------------------------------------------------------

    async def enroll_device(
        self,
        user_id: str,
        org_id: str,
        role: OrgRole,
        request: DeviceEnrollRequest,
    ) -> DeviceTokenResponse:
        """
        Device 등록 + Token 발급.

        Token은 raw 값을 Extension(OS Keychain)에 저장하고,
        DB에는 SHA-256 hash만 저장한다.
        """
        raw_token = secrets.token_urlsafe(48)
        token_hash = self._hash_token(raw_token)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=_DEVICE_TOKEN_TTL_HOURS)

        device = Device(
            user_id=user_id,
            org_id=org_id,
            display_name=request.display_name,
            os_type=request.os_type,
            vscode_version=request.vscode_version or "",
            extension_version=request.extension_version or "",
            token_hash=token_hash,
            status=DeviceStatus.ACTIVE,
            expires_at=expires_at,
        )
        self._db.add(device)
        await self._db.flush()

        logger.info(
            "Device enrolled: %s for user %s (org %s, expires %s)",
            device.device_id, user_id, org_id, expires_at.isoformat(),
        )

        return DeviceTokenResponse(
            device_id=device.device_id,
            token=raw_token,
            expires_at=expires_at,
            org_id=org_id,
            user_id=user_id,
            role=role,
        )

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def process_heartbeat(
        self,
        raw_token: str,
        request: DeviceHeartbeatRequest,
    ) -> DeviceHeartbeatResponse:
        """
        1분마다 Extension → Control Plane heartbeat.

        - revoked / expired → status 반환, Extension이 즉시 차단
        - active → last_heartbeat_at 갱신
        - 최신 policy_bundle_version 포함
        """
        device = await self._get_device_by_token(raw_token)

        if device is None:
            return DeviceHeartbeatResponse(
                status="revoked",
                device_status=DeviceStatus.REVOKED,
            )

        now = datetime.now(timezone.utc)

        # Expired check
        if device.expires_at < now:
            device.status = DeviceStatus.EXPIRED
            await self._db.flush()
            return DeviceHeartbeatResponse(
                status="expired",
                device_status=DeviceStatus.EXPIRED,
            )

        # Revoked check
        if device.status == DeviceStatus.REVOKED:
            return DeviceHeartbeatResponse(
                status="revoked",
                device_status=DeviceStatus.REVOKED,
            )

        # Active — 갱신
        device.last_heartbeat_at = now
        await self._db.flush()

        # 최신 policy bundle version 조회 (있을 경우)
        policy_version = await self._get_latest_policy_version(device.org_id)

        return DeviceHeartbeatResponse(
            status="ok",
            device_status=DeviceStatus.ACTIVE,
            policy_bundle_version=policy_version,
        )

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    async def rotate_device_token(
        self,
        device_id: str,
        org_id: str,
    ) -> Optional["DeviceTokenResponse"]:
        """
        Device Token 회전 (ADR-006).

        - 새 raw_token / token_hash 발급
        - expires_at 갱신 (현재 시각 기준 _DEVICE_TOKEN_TTL_HOURS 후)
        - 이전 token 은 즉시 무효화 (token_hash 가 교체되므로 validate_token() 실패)
        - 회전 사유 / 시각을 Device 객체에 기록

        반환: 새 토큰을 담은 DeviceTokenResponse. 대상 Device 가 없거나 다른 org
        이면 None.
        """
        result = await self._db.execute(
            select(Device).where(
                Device.device_id == device_id,
                Device.org_id == org_id,
            )
        )
        device = result.scalar_one_or_none()
        if device is None:
            return None

        if device.status != DeviceStatus.ACTIVE:
            logger.warning(
                "rotate_device_token rejected: device %s is %s, not ACTIVE",
                device_id, device.status.value,
            )
            return None

        raw_token = secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        new_expires_at = now + timedelta(hours=_DEVICE_TOKEN_TTL_HOURS)

        device.token_hash = self._hash_token(raw_token)
        device.expires_at = new_expires_at
        # last_heartbeat_at 은 회전한다고 갱신하지 않는다 (실제 연결과 무관)
        await self._db.flush()

        # 멤버 role 조회 (응답에 포함하기 위해)
        from control_plane.services.org_service import OrgService
        role = await OrgService(self._db).get_member_role(device.org_id, device.user_id) or OrgRole.DEVELOPER

        logger.info("Device token rotated: device=%s org=%s", device_id, org_id)
        return DeviceTokenResponse(
            device_id=device.device_id,
            token=raw_token,
            expires_at=new_expires_at,
            org_id=device.org_id,
            user_id=device.user_id,
            role=role,
        )

    async def revoke_device(
        self,
        device_id: str,
        reason: str,
        mark_lost: bool = False,
    ) -> bool:
        """Device 즉시 폐기. 다음 heartbeat에서 Extension이 차단된다."""
        result = await self._db.execute(
            select(Device).where(Device.device_id == device_id)
        )
        device = result.scalar_one_or_none()
        if device is None:
            return False

        now = datetime.now(timezone.utc)
        device.status = DeviceStatus.LOST if mark_lost else DeviceStatus.REVOKED
        device.revoked_at = now
        device.revoke_reason = reason
        await self._db.flush()

        logger.warning(
            "Device revoked: %s (reason: %s, lost: %s)",
            device_id, reason, mark_lost,
        )
        return True

    # ------------------------------------------------------------------
    # Token validation (미들웨어에서 사용)
    # ------------------------------------------------------------------

    async def validate_token(self, raw_token: str) -> Optional[Device]:
        """
        API 요청마다 Device Token을 검증한다.
        revoked / expired이면 None 반환.
        """
        device = await self._get_device_by_token(raw_token)
        if device is None:
            return None
        now = datetime.now(timezone.utc)
        if device.status != DeviceStatus.ACTIVE or device.expires_at < now:
            return None
        return device

    # ------------------------------------------------------------------
    # Offline level check
    # ------------------------------------------------------------------

    def check_offline_allowed(
        self, device: Device, level: str
    ) -> tuple[bool, str]:
        """
        오프라인 모드 허용 여부 판단 (설계서 §오프라인 모드 정책).

        Returns (allowed: bool, reason: str)
        """
        if level in ("level_1_2",):
            return True, "항상 허용"

        if level == "production":
            return False, "프로덕션 배포는 항상 온라인 승인 필요"

        if level == "level_4":
            return False, "Level 4 민감 변경은 항상 차단"

        if level == "level_3":
            # 마지막 heartbeat 1시간 이내 확인
            if device.last_heartbeat_at is None:
                return False, "heartbeat 기록 없음 — 온라인 연결 필요"
            now = datetime.now(timezone.utc)
            elapsed = (now - device.last_heartbeat_at).total_seconds() / 3600
            if elapsed > _HEARTBEAT_OFFLINE_LIMIT_HOURS:
                return False, f"마지막 heartbeat {elapsed:.1f}시간 전 — 1시간 초과"
            return True, "정책 캐시 유효 + heartbeat 1시간 이내"

        return False, f"알 수 없는 레벨: {level}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_device_by_token(self, raw_token: str) -> Optional[Device]:
        token_hash = self._hash_token(raw_token)
        result = await self._db.execute(
            select(Device).where(Device.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode()).hexdigest()

    async def _get_latest_policy_version(self, org_id: str) -> Optional[str]:
        """PolicyBundle 최신 버전 조회 (Q2-B 구현)"""
        from control_plane.services.policy_service import PolicyService
        policy_svc = PolicyService(self._db)
        return await policy_svc.get_latest_version(org_id)
