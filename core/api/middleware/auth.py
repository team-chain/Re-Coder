"""
ReCoder Core — Session Token Authentication Middleware

Validates the X-Session-Token header on every request except /api/health.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


# Paths that do NOT require authentication
_EXEMPT_PATHS = {"/api/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}


class SessionTokenMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces X-Session-Token header authentication.

    The expected token is injected at application startup and stored in
    ``app.state.session_token``.
    """

    async def dispatch(self, request: Request, call_next):
        # Allow health-check endpoint without authentication
        if request.url.path in _EXEMPT_PATHS:
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
        import hmac
        if not hmac.compare_digest(provided_token, expected_token):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid session token."},
            )

        return await call_next(request)
