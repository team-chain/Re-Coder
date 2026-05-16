"""
ReCoder Core — Ops / Incident Response Routes

SSH-based incident fetch, AI-powered analysis, and remote remediation.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from schemas import (
    ActionType,
    AlertRecord,
    AlertType,
    ApprovalLevel,
    ResponseProposal,
    RiskLevel,
)

router = APIRouter(tags=["ops"])

# ---------------------------------------------------------------------------
# In-process stores
# ---------------------------------------------------------------------------

_proposals: dict[str, ResponseProposal] = {}
_alerts: dict[str, AlertRecord] = {}

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class FetchIncidentsRequest(BaseModel):
    host: str
    ssh_key_path: str
    ssh_user: str = "ec2-user"
    ssh_port: int = 22
    incident_log_path: str = "/var/log/recoder/incidents.jsonl"
    limit: int = 50


class AnalyzeIncidentRequest(BaseModel):
    alert_id: str
    extra_context: Optional[str] = None


class ApproveResponseRequest(BaseModel):
    proposal_id: str
    approved: bool
    ssh_host: Optional[str] = None
    ssh_user: Optional[str] = None
    ssh_key_path: Optional[str] = None


# ---------------------------------------------------------------------------
# OpsAgent — lazy singleton
# ---------------------------------------------------------------------------

_ops_agent = None  # type: ignore


def _get_ops_agent():
    global _ops_agent
    if _ops_agent is None:
        try:
            from llm.provider_router import LLMProviderRouter  # type: ignore
            from ops_agent import OpsAgent  # type: ignore
            _ops_agent = OpsAgent(provider_router=LLMProviderRouter())
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("OpsAgent unavailable: %s", exc)
    return _ops_agent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ssh_fetch_file(
    host: str,
    user: str,
    key_path: str,
    port: int,
    remote_path: str,
) -> str:
    """Fetch the content of a remote file via SSH."""
    cmd = [
        "ssh",
        "-i", key_path,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-p", str(port),
        f"{user}@{host}",
        f"cat {remote_path}",
    ]
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=30),
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail=f"SSH fetch failed: {result.stderr[:500]}",
            )
        return result.stdout
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="SSH connection timed out.")
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="SSH client not found on this host.")


async def _ssh_exec(
    host: str,
    user: str,
    key_path: str,
    command: str,
    port: int = 22,
) -> dict:
    """Execute a command on a remote host via SSH and return the result."""
    cmd = [
        "ssh",
        "-i", key_path,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-p", str(port),
        f"{user}@{host}",
        command,
    ]
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=60),
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout[:4000],
            "stderr": result.stderr[:2000],
        }
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="SSH command timed out.")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/ops/fetch-incidents")
async def fetch_incidents(request: FetchIncidentsRequest) -> list[AlertRecord]:
    """
    Connect to a remote host via SSH and retrieve incident records from
    the JSONL log file at *incident_log_path*.
    """
    raw_content = await _ssh_fetch_file(
        host=request.host,
        user=request.ssh_user,
        key_path=request.ssh_key_path,
        port=request.ssh_port,
        remote_path=request.incident_log_path,
    )

    records: list[AlertRecord] = []
    for line in raw_content.strip().splitlines()[-request.limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            alert = AlertRecord(**data)
            records.append(alert)
            _alerts[alert.alert_id] = alert
        except Exception:
            # Skip malformed lines
            continue

    return records


@router.post("/api/ops/analyze")
async def analyze_incident(request: AnalyzeIncidentRequest) -> ResponseProposal:
    """
    Analyse a fetched AlertRecord and produce a ResponseProposal.

    Delegates to the OpsAgent when available, otherwise returns a
    structured placeholder.
    """
    alert = _alerts.get(request.alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"Alert '{request.alert_id}' not found.")

    ops_agent = _get_ops_agent()
    if ops_agent is not None:
        proposal = await ops_agent.analyze(alert, extra_context=request.extra_context)
    else:
        # Placeholder heuristic
        action = ActionType.DOCKER_RESTART
        risk = RiskLevel.MEDIUM
        if alert.alert_type in (AlertType.CRASH, AlertType.OOM, AlertType.HEALTH_CHECK_FAIL):
            action = ActionType.DOCKER_RESTART
            risk = RiskLevel.HIGH
        elif alert.alert_type == AlertType.DEPLOY_FAILURE:
            action = ActionType.SSH_DOCKER_ROLLBACK
            risk = RiskLevel.HIGH
        elif alert.alert_type in (AlertType.HIGH_CPU, AlertType.HIGH_MEMORY):
            action = ActionType.DOCKER_LOGS
            risk = RiskLevel.MEDIUM

        proposal = ResponseProposal(
            alert_id=request.alert_id,
            action_type=action,
            target_container=alert.container_name,
            risk_level=risk,
            risk_reasons=["[Placeholder] OpsAgent not yet loaded."],
            approval_level=ApprovalLevel.DOUBLE_CONFIRM,
        )

    _proposals[proposal.alert_id] = proposal
    return proposal


@router.post("/api/ops/approve")
async def approve_response(request: ApproveResponseRequest) -> dict:
    """
    Approve or reject an ops ResponseProposal.

    On approval, resolves the command template and executes it via SSH.
    """
    proposal = _proposals.get(request.proposal_id)
    if proposal is None:
        raise HTTPException(
            status_code=404, detail=f"Proposal '{request.proposal_id}' not found."
        )

    if not request.approved:
        del _proposals[request.proposal_id]
        return {"status": "rejected", "proposal_id": request.proposal_id}

    ssh_host = request.ssh_host or proposal.parameters.get("ssh_host")
    ssh_user = request.ssh_user or proposal.parameters.get("ssh_user", "ec2-user")
    ssh_key = request.ssh_key_path or proposal.parameters.get("ssh_key_path")

    if not ssh_host or not ssh_key:
        raise HTTPException(
            status_code=422,
            detail="ssh_host and ssh_key_path are required for remote execution.",
        )

    # Build the remote command
    if proposal.command_template_id:
        from registry import CommandTemplateRegistry  # type: ignore
        reg = CommandTemplateRegistry()
        try:
            remote_cmd = reg.build_command(proposal.command_template_id, proposal.parameters)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Command build failed: {exc}") from exc
    else:
        # Fallback: compose a docker restart command
        container = proposal.target_container or "app"
        remote_cmd = f"docker restart {container}"

    exec_result = await _ssh_exec(
        host=ssh_host,
        user=ssh_user,
        key_path=ssh_key,
        command=remote_cmd,
    )

    del _proposals[request.proposal_id]

    return {
        "status": "executed" if exec_result["exit_code"] == 0 else "failed",
        "proposal_id": request.proposal_id,
        **exec_result,
    }
