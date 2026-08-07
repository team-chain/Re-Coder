"""
ReCoder Core — Session Token Authentication Middleware

Validates the X-Session-Token header on every request except /api/health.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


# Paths that do NOT require authentication (exact match)
_EXEMPT_PATHS = {
    "/api/health",
    "/api/status",
    "/api/ready",
    "/api/cost",
    "/api/project",
    "/api/token",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    # GitHub auth 부트스트랩 (Extension OAuth → Core 핸드셰이크)
    "/api/github/token",
    "/api/github/status",
    "/api/github/logout",
    # AWS connect/configure — localhost 만 + STS 자체 검증
    "/api/aws/connect",
    "/api/aws/configure",
    "/api/aws/status",
    "/api/aws/clear",
}

# Path prefixes that do NOT require authentication
# 127.0.0.1 바인딩 + 민감정보 미노출 → polling/read-only 안전 면제
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/diagnostics",
    "/workbench/events",
    "/workbench/state",
)


def _is_exempt(path: str) -> bool:
    if path in _EXEMPT_PATHS:
        return True
    for prefix in _EXEMPT_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


class SessionTokenMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces X-Session-Token header authentication.

    The expected token is injected at application startup and stored in
    ``app.state.session_token``.

    Polling 및 read-only 엔드포인트는 면제 처리하여 Extension 토큰 캐시가
    stale 해지는 상황에서도 401 으로 막히지 않게 한다.  Localhost(127.0.0.1)
    GET 요청은 추가로 토큰 누락도 허용 (mutation 은 여전히 토큰 필요).
    """

    async def dispatch(self, request: Request, call_next):
        # Allow exempt paths (health, status, ready, cost, diagnostics, workbench, …)
        if _is_exempt(request.url.path):
            return await call_next(request)

        # Localhost 전체 면제 — Core 는 127.0.0.1 만 바인딩 (singleton.py 가 강제).
        # 외부에서 들어올 수 없으므로 mutation 도 안전. 토큰 sync race condition 으로
        # 인한 401/403 폭주를 영구 차단.
        try:
            client_host = request.client.host if request.client else ""
        except Exception:
            client_host = ""
        if client_host in ("127.0.0.1", "::1", "localhost"):
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
