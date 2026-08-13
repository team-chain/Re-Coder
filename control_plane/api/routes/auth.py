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

import base64
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from pydantic import BaseModel

from control_plane.db.session import get_db
from control_plane.models.schemas import (
    AuditAction,
    AuditEventCreate,
    DeviceEnrollRequest,
    DeviceTokenResponse,
    OIDCCallbackRequest,
    OIDCProvider,
    OIDCTokenResponse,
    OrgRole,
)
from control_plane.services.audit import AuditService
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

# ---------------------------------------------------------------------------
# 임시 저장소 (temp_token / OIDC state)
#
# 기본 백엔드: 프로세스 메모리 (개발 / 단일 인스턴스 운영용).
# 운영에서 다중 인스턴스로 확장할 때는 환경변수 RECODER_AUTH_REDIS_URL 을
# 설정하면 _RedisTempStore 가 자동으로 활성화되어 동일 인터페이스로 동작한다.
# ---------------------------------------------------------------------------

_TEMP_TOKEN_TTL_SECONDS = 300   # 5분
_STATE_TTL_SECONDS = 600        # 10분


class _MemoryTempStore:
    """프로세스 메모리 기반 임시 저장소. 단일 인스턴스 운영 / 개발 환경 기본값."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> Optional[dict[str, Any]]:
        meta = self._store.get(key)
        if meta is None:
            return None
        exp = meta.get("expires_at")
        if exp and exp < datetime.now(timezone.utc):
            self._store.pop(key, None)
            return None
        return meta

    def set(self, key: str, value: dict[str, Any]) -> None:
        self._store[key] = value

    def pop(self, key: str, default: Any = None) -> Optional[dict[str, Any]]:
        return self._store.pop(key, default)

    def claim(self, key: str) -> Optional[dict[str, Any]]:
        """**원자적 소비** — 유효하면 꺼내면서 지운다. 만료·부재면 None.

        get 후 별도 pop 은 두 요청이 같은 토큰으로 동시에 들어왔을 때 둘 다
        통과시킨다(1회용 계약 위반). dict.pop 은 GIL 아래 원자적이라 한
        요청만 값을 가져간다.
        """
        meta = self._store.pop(key, None)
        if meta is None:
            return None
        exp = meta.get("expires_at")
        if exp and exp < datetime.now(timezone.utc):
            return None
        return meta

    def purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        for k, meta in list(self._store.items()):
            if meta.get("expires_at") and meta["expires_at"] < now:
                self._store.pop(k, None)


def _build_temp_store() -> _MemoryTempStore:
    """
    Redis URL 이 설정되어 있으면 _RedisTempStore 로 대체.

    Redis 어댑터는 lazy import 로 처리하여 redis 패키지 미설치 환경에서도
    메모리 fallback 으로 동작하도록 한다.
    """
    redis_url = os.environ.get("RECODER_AUTH_REDIS_URL")
    if not redis_url:
        return _MemoryTempStore()
    try:
        import redis  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "RECODER_AUTH_REDIS_URL 이 설정됐지만 redis 패키지가 없어 메모리 fallback 사용. "
            "pip install redis 후 재시도."
        )
        return _MemoryTempStore()

    return _RedisTempStore(redis.Redis.from_url(redis_url))


class _RedisTempStore:
    """Redis 기반 임시 저장소 — TTL 은 Redis EXPIRE 가 자동 처리."""

    def __init__(self, client: Any, prefix: str = "recoder:auth:") -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str) -> Optional[dict[str, Any]]:
        import json
        raw = self._client.get(self._key(key))
        if raw is None:
            return None
        meta = json.loads(raw)
        if "expires_at" in meta:
            meta["expires_at"] = datetime.fromisoformat(meta["expires_at"])
        return meta

    def set(self, key: str, value: dict[str, Any]) -> None:
        import json
        ttl_sec = None
        exp = value.get("expires_at")
        if isinstance(exp, datetime):
            ttl_sec = max(1, int((exp - datetime.now(timezone.utc)).total_seconds()))
        # datetime 직렬화
        payload = {**value, "expires_at": exp.isoformat() if isinstance(exp, datetime) else exp}
        self._client.set(self._key(key), json.dumps(payload), ex=ttl_sec)

    def pop(self, key: str, default: Any = None) -> Optional[dict[str, Any]]:
        meta = self.get(key)
        if meta is not None:
            self._client.delete(self._key(key))
            return meta
        return default

    #: GET+DEL 을 한 번에 — 두 클라이언트가 같은 키를 동시에 소비하지 못한다.
    _CLAIM_LUA = (
        "local v = redis.call('GET', KEYS[1]) "
        "if v then redis.call('DEL', KEYS[1]) end "
        "return v"
    )

    def claim(self, key: str) -> Optional[dict[str, Any]]:
        """**원자적 소비** — Lua 로 GET+DEL 을 한 왕복에 처리한다.

        get() 후 delete() 두 왕복이면 그 사이에 다른 요청이 같은 토큰을
        읽어 갈 수 있다 — 탈취된 temp_token 을 경합시켜 디바이스를 하나 더
        등록하는 경로다.
        """
        import json
        raw = self._client.eval(self._CLAIM_LUA, 1, self._key(key))
        if raw is None:
            return None
        meta = json.loads(raw)
        if "expires_at" in meta:
            meta["expires_at"] = datetime.fromisoformat(meta["expires_at"])
        exp = meta.get("expires_at")
        if isinstance(exp, datetime) and exp < datetime.now(timezone.utc):
            return None
        return meta

    def purge_expired(self) -> None:
        # Redis EXPIRE 가 자동 만료. 별도 작업 불필요.
        return


_TEMP_TOKEN_STORE = _build_temp_store()
_STATE_STORE = _build_temp_store()


def _purge_expired_states() -> None:
    _STATE_STORE.purge_expired()


def _generate_pkce_pair() -> tuple[str, str]:
    """
    RFC 7636 PKCE S256.
    code_verifier: 43~128자의 URL-safe random
    code_challenge: BASE64URL(SHA256(code_verifier)) without padding
    """
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _purge_expired_temp_tokens() -> None:
    _TEMP_TOKEN_STORE.purge_expired()


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

    CSRF 방지를 위해 state 토큰을 발급하고 _STATE_STORE 에 저장한다.
    callback 에서 동일한 state 가 전달되지 않으면 401 로 거부한다.
    """
    _purge_expired_states()
    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _generate_pkce_pair()
    _STATE_STORE.set(state, {
        "provider": provider.value,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,  # callback 에서 token exchange 시 동봉
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=_STATE_TTL_SECONDS),
    })

    if provider == OIDCProvider.GOOGLE:
        client_id = _get_google_client_id()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            # PKCE S256 — Google OAuth 2.0 가 표준 지원
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
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
            # PKCE S256 — GitHub Apps OAuth 가 옵션 지원. 미지원 시 무시되며 동작에 영향 없음.
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
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

    CSRF 보호: login 단계에서 발급된 state 와 callback 의 state 가 동일해야 한다.
    검증 후 state 는 즉시 폐기한다 (replay 차단).
    """
    _purge_expired_states()
    # 원자적 소비 — Redis 다중 인스턴스에서 pop(get+del)은 두 콜백을
    # 모두 통과시킨다(1회용 계약 위반). claim 은 GET+DEL 을 한 번에.
    state_meta = _STATE_STORE.claim(request.state)
    if state_meta is None:
        raise HTTPException(status_code=401, detail="Invalid or expired OIDC state (CSRF check failed)")
    if state_meta.get("provider") != provider.value:
        raise HTTPException(status_code=401, detail="OIDC state provider mismatch")
    # redirect_uri 도 동일해야 한다 (open-redirect 방지)
    if state_meta.get("redirect_uri") != request.redirect_uri:
        raise HTTPException(status_code=401, detail="OIDC redirect_uri mismatch")

    # PKCE — login 단계에서 발급한 code_verifier 를 token exchange 시 함께 전송한다.
    code_verifier: Optional[str] = state_meta.get("code_verifier")

    if provider == OIDCProvider.GOOGLE:
        email, display_name, oidc_subject = await _exchange_google_code(
            request.code, request.redirect_uri, code_verifier=code_verifier,
        )
    elif provider == OIDCProvider.GITHUB:
        email, display_name, oidc_subject = await _exchange_github_code(
            request.code, request.redirect_uri, code_verifier=code_verifier,
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
    _TEMP_TOKEN_STORE.set(temp_token, {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "expires_at": expires_at,
    })

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


class EnrollDeviceBody(BaseModel):
    """
    /auth/devices/enroll 의 요청 바디.

    이전 버전은 temp_token / org_id 를 query parameter 로 받아 URL 에
    민감 데이터가 노출됐다. 보안상 모두 request body 로 통일한다.
    """
    temp_token: str
    org_id: str
    enroll: DeviceEnrollRequest


@router.post("/devices/enroll", response_model=DeviceTokenResponse)
async def enroll_device(
    body: EnrollDeviceBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DeviceTokenResponse:
    """
    temp_token + org_id + DeviceEnrollRequest → DeviceToken.

    DeviceToken은 OS Keychain에 저장하고 이후 모든 API 요청에 사용한다.
    설계서 §Q2-A1 의 "모든 Device 등록은 AuditLog 에 기록" 도 함께 충족한다.
    """
    _purge_expired_temp_tokens()
    # **원자적 소비.** get 으로 확인만 하고 나중에 pop 하면, 같은 temp_token
    # 을 든 두 요청이 그 틈에 둘 다 통과해 디바이스가 두 개 등록된다 —
    # 1회용 토큰 계약 위반이자, 탈취 토큰의 경합 등록 경로. claim 은 유효할
    # 때만 꺼내면서 지우므로 한 요청만 진행한다. 등록이 실패하면 아래
    # except 에서 되살려 사용자가 재시도할 수 있게 한다.
    token_data = _TEMP_TOKEN_STORE.claim(body.temp_token)
    if token_data is None:
        raise HTTPException(status_code=401, detail="Invalid or expired temp_token")

    user_id = token_data["user_id"]
    org_id = body.org_id

    try:

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
            # **멤버십을 실제로 저장한다.** 역할을 지역 변수로만 들고 가면 토큰은
            # 발급되지만, 이후 모든 요청의 get_current_device() 가
            # get_member_role() == None 으로 거부해 — 조직의 첫 디바이스가
            # 발급 즉시 쓸 수 없는 토큰을 쥐게 된다.
            db.add(OrgMember(org_id=org_id, user_id=user_id, role=role))
            await db.flush()

        svc = IdentityService(db)
        token_response = await svc.enroll_device(
            user_id=user_id,
            org_id=org_id,
            role=role,
            request=body.enroll,
        )


        # AuditLog: device.enrolled — 설계서 §Q2-A3 "Device 등록도 AuditLog 기록 100%"
        try:
            audit_svc = AuditService(db)
            ip = request.client.host if request.client else None
            await audit_svc.record(
                org_id=org_id,
                actor_user_id=user_id,
                actor_device_id=token_response.device_id if hasattr(token_response, "device_id") else None,
                event=AuditEventCreate(
                    action=AuditAction.DEVICE_ENROLLED,
                    resource_type="device",
                    resource_id=getattr(token_response, "device_id", None),
                    ip_address=ip,
                    occurred_at=datetime.now(timezone.utc),
                    extra={"role": role.value, "platform": body.enroll.platform if hasattr(body.enroll, "platform") else None},
                ),
            )
        except Exception as exc:  # AuditLog 실패가 사용자 흐름을 깨지 않도록 fail-soft
            logger.warning("device.enrolled audit log failed: %s", exc)

        return token_response
    except Exception:
        # 등록 실패 — 소비한 claim 을 되살려 사용자가 같은 temp_token 으로
        # 재시도할 수 있게 한다(잘못된 org 선택, 일시적 DB 오류 등). 만료가
        # 지났으면 set 해도 claim 이 걸러낸다. 성공 시엔 소비된 채 남는다.
        try:
            _TEMP_TOKEN_STORE.set(body.temp_token, token_data)
        except Exception:  # noqa: BLE001
            pass
        raise


# ---------------------------------------------------------------------------
# OIDC 코드 교환 헬퍼
# ---------------------------------------------------------------------------

async def _exchange_google_code(
    code: str, redirect_uri: str, code_verifier: Optional[str] = None,
) -> tuple[str, str, str]:
    """Google OIDC code → (email, display_name, sub). PKCE 지원."""
    payload = {
        "client_id": _get_google_client_id(),
        "client_secret": _get_google_client_secret(),
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            _GOOGLE_TOKEN_URL,
            data=payload,
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
    code: str, redirect_uri: str, code_verifier: Optional[str] = None,
) -> tuple[str, str, str]:
    """GitHub OAuth code → (email, display_name, id). PKCE 지원 (GitHub Apps OAuth)."""
    payload = {
        "client_id": _get_github_client_id(),
        "client_secret": _get_github_client_secret(),
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            _GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data=payload,
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
