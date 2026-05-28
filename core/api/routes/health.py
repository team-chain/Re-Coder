"""
ReCoder Core — Health & Diagnostics Routes
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from schemas import DiagnosticsResult

try:
    from first_run import FirstRunDiagnostics  # type: ignore
    _FIRST_RUN_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FIRST_RUN_AVAILABLE = False

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
    if not _FIRST_RUN_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="FirstRunDiagnostics module not available in this build.",
        )
    diag = FirstRunDiagnostics()  # type: ignore[name-defined]
    result = await diag.run_all()
    return result


@router.get("/api/diagnostics")
async def get_diagnostics() -> Optional[DiagnosticsResult]:
    """Return the most recently saved diagnostics result, or null if absent.

    HTTP 204 No Content 는 body 가 없어야 하므로 JSONResponse(content=None) 대신
    body 가 없는 Response(status_code=204) 를 반환한다.
    JSONResponse(status_code=204, content=None) 은 b"null" (4 bytes) 를 body 로
    직렬화하지만 uvicorn 이 204 에 대해 Content-Length 를 0 으로 재설정해
    RuntimeError('Response content longer than Content-Length') 를 발생시킨다.
    """
    if not _FIRST_RUN_AVAILABLE:
        return Response(status_code=204)
    diag = FirstRunDiagnostics()  # type: ignore[name-defined]
    result = await diag.load_diagnostics()
    if result is None:
        return Response(status_code=204)
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
