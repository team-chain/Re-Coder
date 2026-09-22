"""
ReCoder Core — Session Token Authentication Middleware

Validates the X-Session-Token header on every request except /api/health.
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


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
        if not hmac.compare_digest(provided_token.encode("utf-8"), expected_token.encode("utf-8")):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid session token."},
            )

        return await call_next(request)
