"""
ReCoder Core — Session Token Authentication Middleware

Validates the X-Session-Token header on every request except /api/health.
"""

from __future__ import annotations

import hmac
import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


#: CI 토큰(GitHub Actions Secret RECODER_CI_TOKEN)이 부를 수 있는 유일한 경로.
CI_TOKEN_ROUTES = frozenset({("POST", "/workbench/preflight/run")})

#: 연동 토큰(Discord 봇)이 부를 수 있는 경로 — 봇이 실제로 쓰는 것만(discord-bot/recoder_client.py).
#: 자격증명 저장 · GitHub 토큰/Secret · git push · AWS 연결 · 토큰 조회 같은 경로는 열지 않는다.
INTEGRATION_ROUTES = frozenset({
    "/api/analyze", "/api/ecs/deploy", "/api/ecs/rollback", "/api/forecast/deploy", "/api/incident/timeline",
    "/api/preflight/run", "/gitops/rollback-pr", "/gitops/sync", "/gitops/syncs",
    "/workbench/deployment/start", "/workbench/deployments", "/workbench/events", "/workbench/mode",
    "/workbench/preflight/run", "/workbench/state",
})
#: 뒤에 ID 가 붙는 봇 경로 — 모양을 정확히 맞춘다(같은 접두어에 나중에 생기는 경로가 열리지 않게).
_INTEGRATION_PATTERNS = (
    re.compile(r"/gitops/rollback-pr/[A-Za-z0-9_.-]{1,128}"),
    re.compile(r"/gitops/sync/[A-Za-z0-9_.-]{1,128}"),
    re.compile(r"/workbench/deployment/[A-Za-z0-9_.-]{1,128}/rollback"),
)


def integration_allowed(path: str) -> bool:
    if ".." in path:
        return False
    return path in INTEGRATION_ROUTES or any(p.fullmatch(path) for p in _INTEGRATION_PATTERNS)


class SessionTokenMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces X-Session-Token header authentication.

    The expected token is injected at application startup and stored in
    ``app.state.session_token``.

    Only GET /api/health is public for startup discovery. Loopback requests,
    polling and credential setup still require a token from runtime.json.
    CORS preflight is handled by the outer CORSMiddleware, without invoking
    an endpoint. It does not exempt the subsequent request from authentication.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method == "GET" and request.url.path == "/api/health":
            return await call_next(request)

        # Extract and validate the session token
        provided_token = request.headers.get("X-Session-Token", "")
        expected_token: str = getattr(request.app.state, "session_token", "")

        if not expected_token:
            # Token not yet initialised — deny all authenticated routes
            return JSONResponse(
                status_code=503,
                content={"detail": "Server not ready: session token not initialised."},
            )

        if not provided_token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing X-Session-Token header."},
            )

        # Constant-time comparison to prevent timing attacks
        if hmac.compare_digest(provided_token.encode("utf-8"), expected_token.encode("utf-8")):
            return await call_next(request)

        # CI token: accepted only for the Preflight gate route, never elsewhere.
        ci_token: str = getattr(request.app.state, "ci_token", "") or ""
        if (
            ci_token
            and (request.method.upper(), request.url.path) in CI_TOKEN_ROUTES
            and hmac.compare_digest(provided_token.encode("utf-8"), ci_token.encode("utf-8"))
        ):
            return await call_next(request)

        # 연동 토큰(3.0.2): 세션 토큰은 Core 를 켤 때마다 바뀌므로, Discord 봇에 넣어 두는 값은 따로 영속한다.
        # 봇이 쓰는 경로만 연다(INTEGRATION_ROUTES). 새로 만들기: POST /api/session/integration-token/rotate(세션 토큰).
        integration: str = getattr(request.app.state, "integration_token", "") or ""
        if (
            integration
            and integration_allowed(request.url.path)
            and hmac.compare_digest(provided_token.encode("utf-8"), integration.encode("utf-8"))
        ):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid session token."},
        )
