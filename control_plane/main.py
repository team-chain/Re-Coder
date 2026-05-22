"""
ReCoder Control Plane — FastAPI 애플리케이션

설계서 §Q2-A:
- 포트: 18000 (기본값, 환경변수로 오버라이드)
- OIDC + Device Token 인증
- 멀티테넌트 org_id 격리
- AuditLog 기록률 100%
- Offline Level 1~2 허용, Level 3~4 / 프로덕션 항상 차단
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from control_plane.api.routes import auth, audit, devices, orgs, policy, approvals
from control_plane.db.migrations import init_db

logger = logging.getLogger(__name__)

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


# ---------------------------------------------------------------------------
# Lifespan: DB 초기화
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Control Plane starting — initializing DB...")
    apply_rls = os.environ.get("APPLY_RLS", "true").lower() == "true"
    try:
        await init_db(apply_rls=apply_rls)
        logger.info("DB initialized")
    except Exception as exc:
        logger.error("DB init failed: %s", exc)
        # DB 없이는 시작 불가 — 그대로 raise
        raise
    yield
    logger.info("Control Plane shutting down")


# ---------------------------------------------------------------------------
# FastAPI 앱
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ReCoder Control Plane",
    description="SaaS 멀티테넌트 조직 통제 + 감사 + 인증 API (Q2-A)",
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# CORS (Extension WebView + Web UI)
# ---------------------------------------------------------------------------

_ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:5173,vscode-webview://*",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 전역 예외 핸들러
# ---------------------------------------------------------------------------

@app.exception_handler(PermissionError)
async def permission_error_handler(request: Request, exc: PermissionError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": str(exc)},
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


# ---------------------------------------------------------------------------
# 라우터 등록
# ---------------------------------------------------------------------------

app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(orgs.router)
app.include_router(audit.router)
app.include_router(policy.router)
app.include_router(approvals.router)


# ---------------------------------------------------------------------------
# 헬스체크
# ---------------------------------------------------------------------------

@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok", "service": "recoder-control-plane", "version": "0.2.0"}


@app.get("/", tags=["health"])
async def root() -> dict:
    return {
        "service": "ReCoder Control Plane",
        "docs": "/docs",
        "version": "0.2.0",
    }


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("CONTROL_PLANE_PORT", "18000"))
    uvicorn.run(
        "control_plane.main:app",
        host="0.0.0.0",
        port=port,
        reload=os.environ.get("DEV_RELOAD", "false").lower() == "true",
        log_level=_LOG_LEVEL.lower(),
    )
