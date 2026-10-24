"""
ReCoder Core — Session Token Authentication Middleware

Validates the X-Session-Token header on every request except /api/health.
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


#: CI 토큰(GitHub Actions Secret RECODER_CI_TOKEN)이 부를 수 있는 유일한 경로.
CI_TOKEN_ROUTES = frozenset({("POST", "/workbench/preflight/run")})

#: 연동 토큰(Discord 봇 등 사용자가 직접 연결한 도구)이 **부를 수 없는** 경로 — 토큰을 꺼내 보는 곳.
INTEGRATION_BLOCKED_PREFIXES = ("/api/session/",)


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

        # 연동 토큰(3.0.2): 세션 토큰은 Core 를 켤 때마다 바뀌므로, 사용자가 Discord 봇 등에 직접 넣어 두는 값은
        # 따로 영속한다. 토큰을 꺼내 보는 /api/session/* 은 부를 수 없다. AWS 를 바꾸는 요청은 어차피 approved 가 필요하다.
        integration: str = getattr(request.app.state, "integration_token", "") or ""
        if (
            integration
            and not request.url.path.startswith(INTEGRATION_BLOCKED_PREFIXES)
            and hmac.compare_digest(provided_token.encode("utf-8"), integration.encode("utf-8"))
        ):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid session token."},
        )
