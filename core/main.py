"""
ReCoder Local Core — FastAPI Entry Point

Binds exclusively to 127.0.0.1, enforces session-token authentication,
manages the process singleton, and coordinates graceful startup/shutdown.
"""

from __future__ import annotations

import os
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

_CORE_DIR = Path(__file__).parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from singleton import CoreSingleton
from api.middleware.auth import SessionTokenMiddleware
from api.routes import health, analyze, deploy, ops, session
from api.routes import policy
from api.routes import ecs
from api.routes import gitops
from api.routes import incident

_bound_port: int = 0
VERSION = "1.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    pid = os.getpid()

    lock_acquired = CoreSingleton.acquire_lock(pid)
    if not lock_acquired:
        existing = CoreSingleton.read_runtime()
        if existing:
            app.state.port = existing.port
            app.state.session_token = existing.session_token
        yield
        CoreSingleton.remove_window(pid)
        return

    port = _bound_port if _bound_port else CoreSingleton.find_available_port()
    app.state.port = port

    token = secrets.token_urlsafe(32)
    app.state.session_token = token

    CoreSingleton.write_runtime(port=port, token=token, pid=pid)
    CoreSingleton.set_file_permissions(CoreSingleton.RUNTIME_FILE)
    CoreSingleton.set_file_permissions(CoreSingleton.LOCK_FILE)

    app.state.started_at = datetime.now(timezone.utc)

    # OTel 초기화 (Q4)
    try:
        from observability import observability
        observability.initialize()
    except Exception:
        pass

    yield

    is_last = CoreSingleton.remove_window(pid)
    if is_last:
        CoreSingleton.release_lock(pid)


def create_app() -> FastAPI:
    app = FastAPI(
        title="ReCoder Local Core",
        version=VERSION,
        description="Local AI-assisted development backend for the ReCoder VSCode extension.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1", "http://localhost"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(SessionTokenMiddleware)

    # Q1~Q3 라우터
    app.include_router(health.router)
    app.include_router(analyze.router)
    app.include_router(deploy.router)
    app.include_router(ops.router)
    app.include_router(session.router)
    app.include_router(policy.router)
    app.include_router(ecs.router)
    # Q4 라우터
    app.include_router(gitops.router)
    app.include_router(incident.router)

    return app


app = create_app()


def main() -> None:
    global _bound_port
    try:
        _bound_port = CoreSingleton.find_available_port()
    except RuntimeError as exc:
        print(f"[ReCoder Core] FATAL: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"[ReCoder Core] Starting on http://127.0.0.1:{_bound_port}", flush=True)

    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=_bound_port,
        log_level="info",
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
