"""
Control Plane — Q2-A1: OIDC 인증 라우트

ADR-006: Google/GitHub OIDC만 지원. 비밀번호 기반 인증 없음.

흐름:
  1. GET /auth/{provider}/login  → OIDC Authorization URL로 리다이렉트
  2. GET /auth/{provider}/callback → code → 사용자 조회/생성 → temp_token 발급
  3. POST /auth/devices/enroll    → temp_token + DeviceEnrollRequest → DeviceToken

Device Token은 OS Keychain에 저장한다 (Extension이 처리).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.db.session import get_db
from control_plane.models.schemas import (
    DeviceEnrollRequest,
    DeviceTokenResponse,
    OIDCCallbackRequest,
    OIDCProvider,
    OIDCTokenResponse,
    OrgRole,
)
from control_plane.services.identity import IdentityService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# OIDC Provider 설정
# ---------------------------------------------------------------------------

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_USERINFO_URL = "https://api.github.com/user"
_GITHUB_EMAIL_URL = "https://api.github.com/user/emails"

# 임시 토큰 저장소 (개발용 — 운영에서는 Redis/DB로 교체)
_TEMP_TOKEN_STORE: dict[str, dict[str, Any]] = {}
_TEMP_TOKEN_TTL_SECONDS = 300   # 5분


def _get_google_client_id() -> str:
    v = os.environ.get("GOOGLE_CLIENT_ID", "")
    if not v:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID not configured")
    return v


def _get_google_client_secret() -> str:
    v = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not v:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_SECRET not configured")
    return v


def _get_github_client_id() -> str:
    v = os.environ.get("GITHUB_CLIENT_ID", "")
    if not v:
        raise HTTPException(status_code=500, detail="GITHUB_CLIENT_ID not configured")
    return v


def _get_github_client_secret() -> str:
    v = os.environ.get("GITHUB_CLIENT_SECRET", "")
    if not v:
        raise HTTPException(status_code=500, detail="GITHUB_CLIENT_SECRET not configured")
    return v


# ---------------------------------------------------------------------------
# 1단계: Authorization URL 생성
# ---------------------------------------------------------------------------

@router.get("/{provider}/login")
async def oidc_login(provider: OIDCProvider, redirect_uri: str, request: Request) -> RedirectResponse:
    """
    OIDC Authorization URL로 리다이렉트.
    Extension이 WebView에서 이 URL을 열어 로그인을 진행한다.
    """
    state = secrets.token_urlsafe(32)

    if provider == OIDCProvider.GOOGLE:
        client_id = _get_google_client_id()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
        }
        import urllib.parse
        url = _GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)

    elif provider == OIDCProvider.GITHUB:
        client_id = _get_github_client_id()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "read:user user:email",
            "state": state,
        }
        import urllib.parse
        url = _GITHUB_AUTH_URL + "?" + urllib.parse.urlencode(params)

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")

    return RedirectResponse(url=url)


# ---------------------------------------------------------------------------
# 2단계: Callback → temp_token 발급
# ---------------------------------------------------------------------------

@router.post("/{provider}/callback", response_model=OIDCTokenResponse)
async def oidc_callback(
    provider: OIDCProvider,
    request: OIDCCallbackRequest,
    db: AsyncSession = Depends(get_db),
) -> OIDCTokenResponse:
    """
    OIDC code → access_token → userinfo → User 조회/생성 → temp_token 발급.
    temp_token은 5분 유효. Device enroll 때 사용.
    """
    if provider == OIDCProvider.GOOGLE:
        email, display_name, oidc_subject = await _exchange_google_code(
            request.code, request.redirect_uri
        )
    elif provider == OIDCProvider.GITHUB:
        email, display_name, oidc_subject = await _exchange_github_code(
            request.code, request.redirect_uri
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")

    svc = IdentityService(db)
    user = await svc.get_or_create_user(
        email=email,
        display_name=display_name,
        provider=provider,
        oidc_subject=oidc_subject,
    )

    # temp_token 발급 (5분 유효)
    temp_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_TEMP_TOKEN_TTL_SECONDS)
    _TEMP_TOKEN_STORE[temp_token] = {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "expires_at": expires_at,
    }

    logger.info("OIDC login successful: %s (%s)", email, provider.value)
    return OIDCTokenResponse(
        temp_token=temp_token,
        expires_in=_TEMP_TOKEN_TTL_SECONDS,
        user_id=user.user_id,
        email=email,
        display_name=display_name,
    )


# ---------------------------------------------------------------------------
# 3단계: Device 등록
# ---------------------------------------------------------------------------

@router.post("/devices/enroll", response_model=DeviceTokenResponse)
async def enroll_device(
    temp_token: str,
    org_id: str,
    request: DeviceEnrollRequest,
    db: AsyncSession = Depends(get_db),
) -> DeviceTokenResponse:
    """
    temp_token + org_id + DeviceEnrollRequest → DeviceToken.

    DeviceToken은 OS Keychain에 저장하고 이후 모든 API 요청에 사용한다.
    """
    token_data = _TEMP_TOKEN_STORE.get(temp_token)
    if token_data is None:
        raise HTTPException(status_code=401, detail="Invalid or expired temp_token")

    now = datetime.now(timezone.utc)
    if token_data["expires_at"] < now:
        del _TEMP_TOKEN_STORE[temp_token]
        raise HTTPException(status_code=401, detail="temp_token expired")

    user_id = token_data["user_id"]

    # RBAC: org의 멤버인지 확인 + 역할 조회
    from control_plane.services.org_service import OrgService
    org_svc = OrgService(db)
    role = await org_svc.get_member_role(org_id, user_id)

    if role is None:
        # 최초 등록 시 아직 멤버가 없으면 developer로 자동 등록 (실제 운영에서는 초대 흐름 필수)
        # 이 경우 org에 아무 멤버도 없어야 함
        from control_plane.db.models import OrgMember
        from sqlalchemy import select
        existing = await db.execute(
            select(OrgMember).where(OrgMember.org_id == org_id)
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=403,
                detail="User is not a member of this organization. Request an invite first.",
            )
        role = OrgRole.DEVELOPER

    svc = IdentityService(db)
    token_response = await svc.enroll_device(
        user_id=user_id,
        org_id=org_id,
        role=role,
        request=request,
    )

    # temp_token 즉시 무효화
    del _TEMP_TOKEN_STORE[temp_token]
    return token_response


# ---------------------------------------------------------------------------
# OIDC 코드 교환 헬퍼
# ---------------------------------------------------------------------------

async def _exchange_google_code(
    code: str, redirect_uri: str
) -> tuple[str, str, str]:
    """Google OIDC code → (email, display_name, sub)"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "client_id": _get_google_client_id(),
                "client_secret": _get_google_client_secret(),
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Google token exchange failed")

        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access_token from Google")

        userinfo_resp = await client.get(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Google userinfo fetch failed")

        info = userinfo_resp.json()
        return info["email"], info.get("name", info["email"]), info["sub"]


async def _exchange_github_code(
    code: str, redirect_uri: str
) -> tuple[str, str, str]:
    """GitHub OAuth code → (email, display_name, id)"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            _GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": _get_github_client_id(),
                "client_secret": _get_github_client_secret(),
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="GitHub token exchange failed")

        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access_token from GitHub")

        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

        user_resp = await client.get(_GITHUB_USERINFO_URL, headers=headers)
        if user_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="GitHub userinfo fetch failed")
        user_info = user_resp.json()

        # 이메일이 public이 아닐 수 있으므로 emails API 호출
        email = user_info.get("email")
        if not email:
            email_resp = await client.get(_GITHUB_EMAIL_URL, headers=headers)
            if email_resp.status_code == 200:
                for e in email_resp.json():
                    if e.get("primary") and e.get("verified"):
                        email = e["email"]
                        break
        if not email:
            raise HTTPException(status_code=400, detail="GitHub account has no verified email")

        display_name = user_info.get("name") or user_info.get("login", email)
        oidc_subject = str(user_info["id"])
        return email, display_name, oidc_subject
