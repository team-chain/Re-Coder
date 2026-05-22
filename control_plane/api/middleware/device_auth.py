"""
Control Plane — Device Token 인증 미들웨어

설계서 §Q2-A2:
- 모든 API 엔드포인트에서 미들웨어가 Device Token으로부터 org_id를 추출
- 모든 DB 쿼리에 자동 적용
- revoked / expired 토큰은 즉시 차단

사용:
    from control_plane.api.middleware.device_auth import get_current_device, DeviceContext

    @router.get("/example")
    async def example(ctx: DeviceContext = Depends(get_current_device)):
        ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.db.models import Device
from control_plane.db.session import get_db
from control_plane.models.schemas import OrgRole
from control_plane.services.identity import IdentityService

logger = logging.getLogger(__name__)

# PostgreSQL Row Level Security 가 참조하는 GUC 변수.
# migrations.py 의 RLS 정책이 `current_setting('app.current_org_id')` 를 읽는다.
_RLS_GUC_KEY = "app.current_org_id"


@dataclass
class DeviceContext:
    """인증된 요청의 컨텍스트 (미들웨어에서 주입)"""
    device: Device
    user_id: str
    org_id: str
    role: OrgRole

    @property
    def device_id(self) -> str:
        return self.device.device_id


async def _extract_token(authorization: Optional[str] = Header(None)) -> str:
    """
    Authorization: Bearer <token> 헤더에서 토큰 추출.
    없거나 형식이 잘못됐으면 401.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return parts[1].strip()


async def get_current_device(
    raw_token: str = Depends(_extract_token),
    db: AsyncSession = Depends(get_db),
) -> DeviceContext:
    """
    Depends로 주입하는 인증 의존성.

    1. token_hash로 Device 조회
    2. revoked / expired 확인
    3. OrgMember role 조회
    4. DeviceContext 반환
    """
    svc = IdentityService(db)
    device = await svc.validate_token(raw_token)

    if device is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or revoked device token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # OrgMember에서 role 조회
    from control_plane.services.org_service import OrgService
    org_svc = OrgService(db)
    role = await org_svc.get_member_role(device.org_id, device.user_id)

    if role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Device user is not a member of the associated organization",
        )

    # ──────────────────────────────────────────────────────────────────
    # PostgreSQL Row Level Security: 세션 GUC `app.current_org_id` 를
    # 현재 Device 의 org_id 로 설정해야 migrations.py 의 RLS 정책이
    # 실제로 동작한다. SET LOCAL 을 쓰면 트랜잭션 종료 시 자동 해제된다.
    #
    # PostgreSQL 외 백엔드(예: SQLite — 테스트)는 SET LOCAL 을 지원하지
    # 않으므로 예외를 삼키고 진행한다 — 이 경우 RLS 는 미적용이며,
    # ORM 레벨 org_id 필터링이 1차 방어선이 된다.
    # ──────────────────────────────────────────────────────────────────
    try:
        await db.execute(
            text(f"SET LOCAL {_RLS_GUC_KEY} = :org_id"),
            {"org_id": device.org_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("RLS GUC set skipped (non-postgres backend?): %s", exc)

    return DeviceContext(
        device=device,
        user_id=device.user_id,
        org_id=device.org_id,
        role=role,
    )


def require_permission_dep(permission_str: str):
    """
    특정 Permission을 요구하는 Depends factory.

    사용:
        @router.post("/deploy", dependencies=[Depends(require_permission_dep("deployment:request"))])
    """
    from control_plane.models.schemas import Permission

    permission = Permission(permission_str)

    async def _check(ctx: DeviceContext = Depends(get_current_device)) -> DeviceContext:
        from control_plane.models.schemas import has_permission
        if not has_permission(ctx.role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: '{permission.value}' required, role is '{ctx.role.value}'",
            )
        return ctx

    return _check
