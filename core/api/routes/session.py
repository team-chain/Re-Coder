"""
ReCoder Core — Session & Project Management Routes
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from schemas import (
    CostSummary,
    LLMCallRecord,
    ProjectProfile,
    SessionRecord,
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
