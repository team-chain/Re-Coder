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

# ---------------------------------------------------------------------------
# Ensure the core package directory is on sys.path so that bare
# `from schemas import ...` style imports work regardless of how the
# process is launched.
# ---------------------------------------------------------------------------
_CORE_DIR = Path(__file__).parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from singleton import CoreSingleton
from api.middleware.auth import SessionTokenMiddleware
from api.routes import health, analyze, deploy, ops, session
from api.routes import policy

# ---------------------------------------------------------------------------
# Module-level port variable
# Shared between main() and lifespan() so both use the same port.
# find_available_port() must be called only ONCE (in main()); the lifespan
# hook then reads this variable instead of re-probing, which would risk
# returning a different port if another process grabbed the original port
# between the two calls.
# ---------------------------------------------------------------------------
_bound_port: int = 0

# ---------------------------------------------------------------------------
# Application version
# ---------------------------------------------------------------------------
VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Lifespan context manager (startup + shutdown)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup:
      1. Validate singleton (acquire lock; detect/kill stale process).
      2. Find an available port.
      3. Generate a session token.
      4. Write runtime.json with locked-down file permissions.

    Shutdown:
      1. Release the singleton lock.
      2. Remove runtime.json.
    """
    pid = os.getpid()

    # --- Singleton check ---
    lock_acquired = CoreSingleton.acquire_lock(pid)
    if not lock_acquired:
        # Another live Core is running — read its runtime to expose the port
        existing = CoreSingleton.read_runtime()
        if existing:
            app.state.port = existing.port
            app.state.session_token = existing.session_token
        # We still proceed; add_window was already called inside acquire_lock
        yield
        CoreSingleton.remove_window(pid)
        return

    # --- Port discovery ---
    # Use the port already discovered by main() to guarantee that the port
    # uvicorn bound to and the port written to runtime.json are identical.
    port = _bound_port if _bound_port else CoreSingleton.find_available_port()
    app.state.port = port

    # --- Session token ---
    token = secrets.token_urlsafe(32)
    app.state.session_token = token

    # --- Runtime persistence ---
    CoreSingleton.write_runtime(port=port, token=token, pid=pid)
    CoreSingleton.set_file_permissions(CoreSingleton.RUNTIME_FILE)
    CoreSingleton.set_file_permissions(CoreSingleton.LOCK_FILE)

    app.state.started_at = datetime.now(timezone.utc)

    yield  # --- Server running ---

    # --- Shutdown cleanup ---
    is_last = CoreSingleton.remove_window(pid)
    if is_last:
        CoreSingleton.release_lock(pid)


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(
        title="ReCoder Local Core",
        version=VERSION,
        description="Local AI-assisted development backend for the ReCoder VSCode extension.",
        lifespan=lifespan,
    )

    # ---- CORS (localhost only) ----
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1",
            "http://localhost",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- Session token authentication ----
    app.add_middleware(SessionTokenMiddleware)

    # ---- Routers ----
    app.include_router(health.router)
    app.include_router(analyze.router)
    app.include_router(deploy.router)
    app.include_router(ops.router)
    app.include_router(session.router)
    app.include_router(policy.router)

    return app


app = create_app()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """
    Discover an available port and start the uvicorn server.

    The port is discovered ONCE here and stored in the module-level
    _bound_port variable so that the lifespan hook can write the same
    value to runtime.json without re-probing (which could return a
    different port if another process grabbed the original port first).
    """
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
