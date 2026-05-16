"""
ReCoder Core — Session & Project Management Routes
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from schemas import (
    CostSummary,
    LLMCallRecord,
    ProjectProfile,
    SessionRecord,
    StackType,
)

router = APIRouter(tags=["session"])

_RECODER_DIR = Path.home() / ".recoder"
_SESSIONS_DIR = _RECODER_DIR / "sessions"
_PROJECTS_FILE = _RECODER_DIR / "projects.json"
_LLM_CALLS_FILE = _RECODER_DIR / "llm_calls.jsonl"

# ---------------------------------------------------------------------------
# In-process project store (also persisted to disk)
# ---------------------------------------------------------------------------

_projects: dict[str, ProjectProfile] = {}


def _load_projects() -> None:
    """Load projects from the persistent JSON file into the in-memory store."""
    global _projects
    if not _PROJECTS_FILE.exists():
        _projects = {}
        return
    try:
        raw = json.loads(_PROJECTS_FILE.read_text(encoding="utf-8"))
        _projects = {pid: ProjectProfile(**data) for pid, data in raw.items()}
    except Exception:
        _projects = {}


def _save_projects() -> None:
    """Persist the in-memory project store to disk."""
    _RECODER_DIR.mkdir(parents=True, exist_ok=True)
    _PROJECTS_FILE.write_text(
        json.dumps(
            {pid: p.model_dump(mode="json") for pid, p in _projects.items()},
            indent=2,
        ),
        encoding="utf-8",
    )


# Load on import
_load_projects()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_llm_calls() -> list[LLMCallRecord]:
    """Read all LLM call records from the JSONL log."""
    if not _LLM_CALLS_FILE.exists():
        return []
    calls: list[LLMCallRecord] = []
    for line in _LLM_CALLS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                calls.append(LLMCallRecord(**json.loads(line)))
            except Exception:
                continue
    return calls


def _read_sessions() -> list[SessionRecord]:
    """Read all SessionRecord JSON files from the sessions directory."""
    if not _SESSIONS_DIR.exists():
        return []
    records: list[SessionRecord] = []
    for f in sorted(_SESSIONS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            records.append(SessionRecord(**data))
        except Exception:
            continue
    return records


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/cost")
async def get_cost() -> CostSummary:
    """Primary cost endpoint specified in the v6.4 design doc (§19.5)."""
    return await get_cost_summary()


@router.get("/api/session/cost")
async def get_cost_summary() -> CostSummary:
    """Return rolling daily and monthly LLM cost aggregates."""
    calls = _read_llm_calls()
    now = datetime.now(timezone.utc)
    day_cutoff = now - timedelta(days=1)
    month_cutoff = now - timedelta(days=30)

    daily_usd = sum(
        c.estimated_cost_usd
        for c in calls
        if c.timestamp.replace(tzinfo=timezone.utc) >= day_cutoff
    )
    monthly_usd = sum(
        c.estimated_cost_usd
        for c in calls
        if c.timestamp.replace(tzinfo=timezone.utc) >= month_cutoff
    )

    return CostSummary(
        daily_usd=round(daily_usd, 6),
        monthly_usd=round(monthly_usd, 6),
        call_count=len(calls),
        last_updated=now,
    )


@router.get("/api/session/records")
async def list_sessions() -> list[SessionRecord]:
    """Return all stored session records."""
    return _read_sessions()


@router.delete("/api/session/data")
async def delete_data(older_than_days: int = 30) -> dict:
    """
    Delete session data older than *older_than_days* days from
    ~/.recoder/sessions/.

    Also truncates the LLM call log to entries within the retention window.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    deleted_sessions = 0
    deleted_calls = 0

    # Delete old session files
    if _SESSIONS_DIR.exists():
        for f in _SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                start_time = datetime.fromisoformat(data.get("start_time", ""))
                if start_time.replace(tzinfo=timezone.utc) < cutoff:
                    f.unlink()
                    deleted_sessions += 1
            except Exception:
                continue

    # Trim old LLM call records
    if _LLM_CALLS_FILE.exists():
        kept_lines: list[str] = []
        for line in _LLM_CALLS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec.get("timestamp", ""))
                if ts.replace(tzinfo=timezone.utc) >= cutoff:
                    kept_lines.append(line)
                else:
                    deleted_calls += 1
            except Exception:
                kept_lines.append(line)  # Keep lines we can't parse

        _LLM_CALLS_FILE.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")

    return {
        "status": "ok",
        "deleted_sessions": deleted_sessions,
        "deleted_llm_calls": deleted_calls,
        "older_than_days": older_than_days,
    }


# ---------------------------------------------------------------------------
# Project management
# ---------------------------------------------------------------------------


@router.post("/api/projects")
async def create_project(profile: ProjectProfile) -> ProjectProfile:
    """Register a new project profile and persist it to disk."""
    _projects[profile.project_id] = profile
    _save_projects()
    return profile


@router.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> ProjectProfile:
    """Return the ProjectProfile for a given project ID."""
    project = _projects.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found.")
    return project


@router.get("/api/projects")
async def list_projects() -> list[ProjectProfile]:
    """Return all registered project profiles."""
    return list(_projects.values())


# ---------------------------------------------------------------------------
# Convenience single-project endpoints (design doc §20.1)
# GET  /api/project          — return the profile for a given workspace path
# POST /api/project/scan     — scan workspace, auto-detect stack, upsert profile
# ---------------------------------------------------------------------------


class ScanWorkspaceRequest(BaseModel):
    workspace_path: str
    project_id: Optional[str] = None


@router.post("/api/project/scan")
async def scan_project(request: ScanWorkspaceRequest) -> ProjectProfile:
    """
    Scan a workspace directory and auto-detect the project stack, then
    create (or update) the corresponding ProjectProfile.

    Triggered when:
    - ReCoder Sidebar is activated for the first time (§20.1)
    - User clicks "프로젝트 스캔" button in the sidebar
    - First analysis is run

    Stack detection mirrors the heuristic in deploy.py._detect_stack so
    that both routes agree on the resolved StackType.
    """
    from pathlib import Path as _Path
    import json as _json

    ws = _Path(request.workspace_path)
    project_id = request.project_id or str(uuid.uuid4())

    # --- Stack detection ---
    stack = _detect_stack_for_scan(ws)

    # --- Package manager ---
    package_manager: Optional[str] = None
    if (ws / "requirements.txt").exists() or (ws / "pyproject.toml").exists():
        package_manager = "pip"
    elif (ws / "package.json").exists():
        package_manager = "npm"
        if (ws / "yarn.lock").exists():
            package_manager = "yarn"
        elif (ws / "pnpm-lock.yaml").exists():
            package_manager = "pnpm"

    # --- Run command / port heuristics ---
    default_port = 8000
    default_run_command = ""
    health_check_path = "/health"

    if stack.value.startswith("python"):
        default_run_command = "uvicorn main:app --host 0.0.0.0 --port 8000"
        default_port = 8000
        health_check_path = "/health"
    elif stack.value.startswith("node"):
        default_run_command = "node index.js"
        default_port = 3000
        health_check_path = "/health"

    # --- Dockerfile / compose paths ---
    dockerfile_path: Optional[str] = None
    compose_path: Optional[str] = None
    if (ws / "Dockerfile").exists():
        dockerfile_path = str(ws / "Dockerfile")
    if (ws / "docker-compose.yml").exists():
        compose_path = str(ws / "docker-compose.yml")
    elif (ws / "docker-compose.yaml").exists():
        compose_path = str(ws / "docker-compose.yaml")

    now = datetime.now(timezone.utc)

    # Merge with existing profile if present
    existing = _projects.get(project_id)
    if existing:
        profile = ProjectProfile(
            project_id=project_id,
            workspace_path=str(ws),
            stack=stack,
            package_manager=package_manager or existing.package_manager,
            default_run_command=default_run_command or existing.default_run_command,
            default_port=default_port,
            health_check_path=health_check_path,
            dockerfile_path=dockerfile_path or existing.dockerfile_path,
            compose_path=compose_path or existing.compose_path,
            deployment_target=existing.deployment_target,
            created_at=existing.created_at,
            updated_at=now,
        )
    else:
        profile = ProjectProfile(
            project_id=project_id,
            workspace_path=str(ws),
            stack=stack,
            package_manager=package_manager,
            default_run_command=default_run_command,
            default_port=default_port,
            health_check_path=health_check_path,
            dockerfile_path=dockerfile_path,
            compose_path=compose_path,
            created_at=now,
            updated_at=now,
        )

    _projects[project_id] = profile
    _save_projects()
    return profile


def _detect_stack_for_scan(ws) -> "StackType":
    """Stack detection heuristic — duplicated here to avoid circular imports."""
    from schemas import StackType
    from pathlib import Path as _Path

    ws = _Path(ws)
    if (ws / "requirements.txt").exists() or (ws / "pyproject.toml").exists():
        for f in list(ws.rglob("*.py"))[:20]:  # limit file scan
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
                if "fastapi" in text.lower():
                    return StackType.PYTHON_FASTAPI
                if "flask" in text.lower():
                    return StackType.PYTHON_FLASK
                if "django" in text.lower():
                    return StackType.PYTHON_DJANGO
            except Exception:
                continue
        return StackType.PYTHON_FASTAPI
    if (ws / "package.json").exists():
        try:
            import json as _json
            pkg = _json.loads((ws / "package.json").read_text(encoding="utf-8"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                return StackType.NODE_NEXT
            if "@nestjs/core" in deps:
                return StackType.NODE_NEST
            if "express" in deps:
                return StackType.NODE_EXPRESS
        except Exception:
            pass
        return StackType.NODE_EXPRESS
    if (ws / "go.mod").exists():
        return StackType.GO
    if (ws / "pom.xml").exists() or (ws / "build.gradle").exists():
        return StackType.JAVA_SPRING
    if (ws / "Gemfile").exists():
        return StackType.RUBY_RAILS
    return StackType.UNKNOWN


@router.get("/api/project")
async def get_project_by_workspace(workspace_path: str) -> ProjectProfile:
    """
    Return the ProjectProfile for the given workspace path.

    If multiple profiles share the same workspace_path, the most recently
    updated one is returned. Raises 404 if no profile exists for the path.
    Clients should call POST /api/project/scan first if they haven't
    registered the workspace yet.
    """
    matches = [
        p for p in _projects.values()
        if p.workspace_path == workspace_path
    ]
    if not matches:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No ProjectProfile found for workspace '{workspace_path}'. "
                "Call POST /api/project/scan to create one."
            ),
        )
    # Return the most recently updated profile
    return max(matches, key=lambda p: p.updated_at or p.created_at)
