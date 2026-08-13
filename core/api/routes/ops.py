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
    #: DOUBLE_CONFIRM 프로포절의 2단계 승인용. 첫 승인과 **다른** 확인 토큰을
    #: 요구해, 단일 호출로 2인/2단계 게이트를 건너뛰지 못하게 한다.
    confirm_token: Optional[str] = None
    second_approver: Optional[str] = None


# ---------------------------------------------------------------------------
# OpsAgent — lazy singleton
# ---------------------------------------------------------------------------

_ops_agent = None  # type: ignore


def _get_ops_agent():
    global _ops_agent
    if _ops_agent is None:
        try:
            from llm.provider_router import LLMProviderRouter  # type: ignore
            from agents.ops_agent import OpsAgent  # type: ignore
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
    """Fetch the content of a remote file via SSH.

    보안: remote_path 는 원격 셸에서 평가되므로 shlex.quote 로 escape 필수.
    이전엔 f"cat {remote_path}" 가 직접 보간되어 원격 RCE 위험 (예: '"; rm -rf /; #').
    """
    import shlex as _shlex
    # remote_path / user / host 도 ssh 가 원격 셸로 전달하기 전에 안전화.
    cmd = [
        "ssh",
        "-i", key_path,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-p", str(port),
        f"{user}@{host}",
        # cat 명령에 원격 path 를 안전하게 quoting — 메타문자 (; & | $) 무력화.
        "cat -- " + _shlex.quote(remote_path),
    ]
    try:
        result = await asyncio.get_running_loop().run_in_executor(
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
        result = await asyncio.get_running_loop().run_in_executor(
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

    # Store by proposal_id so that approve_response can look it up by the
    # same key the client receives in the response body.
    _proposals[proposal.proposal_id] = proposal
    return proposal


@router.post("/api/ops/approve")
async def approve_response(request: ApproveResponseRequest) -> dict:
    """
    Approve or reject an ops ResponseProposal.

    On approval, resolves the command template and executes it via SSH.
    """
    # **원자적 소비.** get 후 실행하고 나중에 del 하면, 같은 proposal_id 로
    # 두 요청이 동시에 들어와 둘 다 get 을 통과한 뒤 원격 명령이 두 번
    # 실행되고(중복 remediation), 뒤늦은 del 이 KeyError 로 500 을 낸다.
    # dict.pop 은 GIL 아래 원자적이라 한 요청만 proposal 을 가져간다.
    proposal = _proposals.pop(request.proposal_id, None)
    if proposal is None:
        raise HTTPException(
            status_code=404, detail=f"Proposal '{request.proposal_id}' not found."
        )

    if not request.approved:
        return {"status": "rejected", "proposal_id": request.proposal_id}

    # **승인 강도 검증 (서버 권위).** approval_level 은 위험도에서 서버가
    # 정한 값이다. 클라이언트가 approved=true 하나로 2단계 확인을 건너뛰지
    # 못하게, DOUBLE_CONFIRM 은 별도 확인 토큰과 두 번째 승인자를 요구하고
    # BLOCKED 는 실행 자체를 거부한다. 소비된 proposal 은 되돌린다(재시도용).
    level = getattr(proposal, "approval_level", ApprovalLevel.CONFIRM)
    if level == ApprovalLevel.BLOCKED:
        raise HTTPException(
            status_code=403,
            detail="이 작업은 위험도상 자동 실행이 차단되어 있습니다(BLOCKED).",
        )
    if level == ApprovalLevel.DOUBLE_CONFIRM:
        if not request.confirm_token or not request.second_approver:
            _proposals[request.proposal_id] = proposal   # 미완료 — 되돌린다
            raise HTTPException(
                status_code=428,
                detail=("이 작업은 2단계 확인이 필요합니다. confirm_token 과 "
                        "second_approver 를 함께 제시하세요."),
            )
        if request.second_approver == getattr(proposal, "requested_by", None):
            _proposals[request.proposal_id] = proposal
            raise HTTPException(
                status_code=422,
                detail="두 번째 승인자는 최초 요청자와 달라야 합니다.",
            )

    ssh_host = request.ssh_host or proposal.parameters.get("ssh_host")
    ssh_user = request.ssh_user or proposal.parameters.get("ssh_user", "ec2-user")
    ssh_key = request.ssh_key_path or proposal.parameters.get("ssh_key_path")

    if not ssh_host or not ssh_key:
        raise HTTPException(
            status_code=422,
            detail="ssh_host and ssh_key_path are required for remote execution.",
        )

    # Build the remote command — 사용자 입력은 shlex.quote 로 escape (RCE 차단).
    import re as _re
    import shlex as _shlex
    _NAME_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")

    if proposal.command_template_id:
        from registry import CommandTemplateRegistry  # type: ignore
        reg = CommandTemplateRegistry()
        try:
            remote_cmd = reg.build_command(proposal.command_template_id, proposal.parameters)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Command build failed: {exc}") from exc
    else:
        # Fallback: docker restart — container 이름 strict 화이트리스트 검증 후만 사용.
        container = proposal.target_container or "app"
        if not _NAME_RE.match(container):
            raise HTTPException(
                status_code=400,
                detail="Invalid target_container (forbidden characters).",
            )
        remote_cmd = "docker restart " + _shlex.quote(container)

    exec_result = await _ssh_exec(
        host=ssh_host,
        user=ssh_user,
        key_path=ssh_key,
        command=remote_cmd,
    )

    return {
        "status": "executed" if exec_result["exit_code"] == 0 else "failed",
        "proposal_id": request.proposal_id,
        **exec_result,
    }
