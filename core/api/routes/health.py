"""
ReCoder Core — Health & Diagnostics Routes
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from first_run import FirstRunDiagnostics
from schemas import DiagnosticsResult

router = APIRouter(tags=["health"])

_START_TIME: datetime = datetime.now(timezone.utc)


@router.get("/api/health")
async def health(request: Request) -> dict:
    """
    Return basic liveness information.

    This endpoint is exempt from session-token authentication so the
    extension can poll it during startup.
    """
    now = datetime.now(timezone.utc)
    uptime_seconds = (now - _START_TIME).total_seconds()
    port: int = getattr(request.app.state, "port", 0)
    return {
        "status": "ok",
        "version": "1.0.0",
        "uptime_seconds": round(uptime_seconds, 2),
        "port": port,
    }


@router.get("/api/status")
async def get_status(request: Request) -> dict:
    """
    Return the current Orchestrator FSM state and server metadata.

    This is the primary polling target for the VSCode Extension
    (PollingService calls this every 3-5 s to refresh the sidebar).
    The endpoint is intentionally lightweight — no DB/LLM calls.
    """
    now = datetime.now(timezone.utc)
    uptime_seconds = (now - _START_TIME).total_seconds()
    port: int = getattr(request.app.state, "port", 0)

    # Read orchestrator state without instantiating a new one
    orchestrator_state = "idle"
    current_proposal_id: Optional[str] = None
    try:
        # analyze.py keeps a module-level singleton we can inspect
        from api.routes.analyze import _orchestrator  # type: ignore
        if _orchestrator is not None:
            orchestrator_state = _orchestrator.state.value
            current_proposal_id = getattr(_orchestrator, "_current_proposal_id", None)
    except Exception:
        pass

    return {
        "status": "ok",
        "version": "1.0.0",
        "uptime_seconds": round(uptime_seconds, 2),
        "port": port,
        "orchestrator_state": orchestrator_state,
        "current_proposal_id": current_proposal_id,
        "timestamp": now.isoformat(),
    }


@router.post("/api/diagnostics/run")
async def run_diagnostics() -> DiagnosticsResult:
    """
    Execute the full First Run diagnostics suite and return the result.

    Results are also persisted to ~/.recoder/diagnostics.json.
    """
    diag = FirstRunDiagnostics()
    result = await diag.run_all()
    return result


@router.get("/api/diagnostics")
async def get_diagnostics() -> Optional[DiagnosticsResult]:
    """Return the most recently saved diagnostics result, or null if absent."""
    diag = FirstRunDiagnostics()
    result = await diag.load_diagnostics()
    if result is None:
        return JSONResponse(status_code=204, content=None)
    return result


@router.post("/api/shutdown")
async def shutdown(request: Request) -> dict:
    """
    Trigger a graceful shutdown of the Core server.

    Sends SIGTERM to the current process; the uvicorn shutdown hook will
    clean up the singleton lock and runtime files.
    """

    async def _delayed_shutdown():
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_delayed_shutdown())
    return {"status": "shutting_down"}
