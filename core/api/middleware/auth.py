"""
ReCoder Core — Session Token Authentication Middleware

Validates the X-Session-Token header on every request except /api/health.

순수 ASGI 미들웨어로 구현 — BaseHTTPMiddleware 의 알려진 버그
(streaming response 에서 Content-Length 불일치 → RuntimeError) 를 회피한다.
ref: https://github.com/encode/starlette/issues/1012
"""

from __future__ import annotations

import hmac
from typing import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


# Paths that do NOT require authentication
_EXEMPT_PATHS = {"/api/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}


class SessionTokenMiddleware:
    """
    Pure-ASGI middleware that enforces X-Session-Token header authentication.

    BaseHTTPMiddleware 를 상속하지 않으므로 스트리밍 응답에서도 안전하다.
    The expected token is injected at application startup and stored in
    ``app.state.session_token``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # HTTP 요청만 처리 (WebSocket 등은 그대로 통과)
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # 인증 면제 경로
        if path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        # 헤더에서 토큰 추출 (bytes → str)
        headers: dict[bytes, bytes] = dict(scope.get("headers", []))
        provided_token = headers.get(b"x-session-token", b"").decode("utf-8", errors="replace")

        # app.state 에서 기대 토큰 읽기 (scope["app"] 은 Starlette Router 가 주입)
        starlette_app = scope.get("app")
        expected_token: str = getattr(getattr(starlette_app, "state", None), "session_token", "")

        if not expected_token:
            response = JSONResponse(
                status_code=503,
                content={"detail": "Server not ready: session token not initialised."},
            )
            await response(scope, receive, send)
            return

        if not provided_token:
            response = JSONResponse(
                status_code=401,
                content={"detail": "Missing X-Session-Token header."},
            )
            await response(scope, receive, send)
            return

        # 타이밍 공격 방지를 위한 상수 시간 비교
        if not hmac.compare_digest(provided_token, expected_token):
            response = JSONResponse(
                status_code=401,
                content={"detail": "Invalid session token."},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
