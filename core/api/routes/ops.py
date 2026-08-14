"""
ReCoder Core — Ops / Incident Response Routes

SSH-based incident fetch, AI-powered analysis, and remote remediation.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
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


class _SSHNotDispatchedError(RuntimeError):
    """The local SSH process could not start; remote execution is impossible."""


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
    #: DOUBLE_CONFIRM 프로포절의 2단계 승인용 서버 발급 일회용 토큰.
    confirm_token: Optional[str] = None


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


def _authenticated_ops_actor(
    x_ops_actor_token: Optional[str] = Header(
        default=None,
        alias="X-Ops-Actor-Token",
    ),
) -> str:
    """Map an opaque credential to a server-controlled approver identity.

    Configure ``RECODER_OPS_APPROVER_TOKENS`` as a JSON object such as
    ``{"alice":"token-a","bob":"token-b"}``. Tokens must be unique so a
    DOUBLE_CONFIRM decision always represents two independent credentials.
    """
    raw = os.getenv("RECODER_OPS_APPROVER_TOKENS", "").strip()
    if not raw:
        raise HTTPException(
            status_code=503,
            detail="Ops approver credentials are not configured.",
        )
    try:
        configured = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Ops approver credentials are misconfigured.",
        ) from exc
    if not isinstance(configured, dict):
        raise HTTPException(
            status_code=503,
            detail="Ops approver credentials are misconfigured.",
        )

    approvers: dict[str, str] = {}
    for actor, token in configured.items():
        if isinstance(actor, str) and actor.strip() and isinstance(token, str) and token:
            approvers[actor.strip()] = token
    if len(approvers) < 2 or len(set(approvers.values())) != len(approvers):
        raise HTTPException(
            status_code=503,
            detail="Ops approver credentials are misconfigured.",
        )
    if not x_ops_actor_token:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Ops-Actor-Token header.",
        )
    for actor, expected in approvers.items():
        if secrets.compare_digest(
            x_ops_actor_token.encode("utf-8"),
            expected.encode("utf-8"),
        ):
            return actor
    raise HTTPException(status_code=401, detail="Invalid ops approver credential.")


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
    except FileNotFoundError as exc:
        # ssh 실행 파일조차 시작되지 않았으므로 원격 명령은 확실히 미전송이다.
        raise _SSHNotDispatchedError("SSH client not found on this host.") from exc


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
async def analyze_incident(
    request: AnalyzeIncidentRequest,
    authenticated_actor: str = Depends(_authenticated_ops_actor),
) -> ResponseProposal:
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

    # DOUBLE_CONFIRM 은 **서버가 발급한 일회용 토큰**을 요구한다. 클라이언트가
    # 아무 문자열이나 넣어 2단계를 건너뛰지 못하게, 여기서 예측 불가한 토큰을
    # 만들어 proposal 에 심는다(응답에는 exclude 되어 나가지 않는다 — 별도
    # 필드로 최초 요청자에게만 전달). 최초 요청자 신원도 함께 고정한다.
    # Bind the proposal to the identity derived from server-side credentials,
    # never to a caller-controlled request-body string.
    proposal.requested_by = authenticated_actor
    confirm_token_plain = None
    if proposal.approval_level == ApprovalLevel.DOUBLE_CONFIRM:
        confirm_token_plain = secrets.token_urlsafe(24)
        proposal.confirm_token = confirm_token_plain

    # Store by proposal_id so that approve_response can look it up by the
    # same key the client receives in the response body.
    _proposals[proposal.proposal_id] = proposal

    # confirm_token 원문은 응답의 별도 필드로 **최초 요청자에게만** 반환한다
    # (proposal.confirm_token 은 exclude 라 직렬화되지 않는다). 이후 approve
    # 단계에서 두 번째 승인자가 이 토큰을 제시해야 실행된다.
    if confirm_token_plain is not None:
        data = proposal.model_dump()
        data["confirm_token"] = confirm_token_plain
        return JSONResponse(content=jsonable_encoder(data))
    return proposal


@router.post("/api/ops/approve")
async def approve_response(
    request: ApproveResponseRequest,
    authenticated_actor: str = Depends(_authenticated_ops_actor),
) -> dict:
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

    # 여기부터 실행 전 어떤 실패든 proposal 을 **되돌려** 재시도 가능하게 한다.
    # pop 으로 원자 소비했으므로, 복원하지 않으면 SSH 파라미터 누락·템플릿
    # 오류·일시적 예외 후 재시도가 404 가 된다(P2).
    try:
        # **승인 강도 검증 (서버 권위).** approval_level 은 위험도에서 서버가
        # 정한 값이다. DOUBLE_CONFIRM 은 서버가 발급한 일회용 confirm_token 과
        # 최초 요청자와 다른 두 번째 승인자를 요구하고, BLOCKED 는 거부한다.
        level = getattr(proposal, "approval_level", ApprovalLevel.CONFIRM)
        if level == ApprovalLevel.BLOCKED:
            raise HTTPException(
                status_code=403,
                detail="이 작업은 위험도상 자동 실행이 차단되어 있습니다(BLOCKED).",
            )
        if level == ApprovalLevel.DOUBLE_CONFIRM:
            issued = getattr(proposal, "confirm_token", None)
            requester = (proposal.requested_by or "").strip()
            if not requester:
                raise HTTPException(
                    status_code=409,
                    detail="최초 요청자의 인증 신원이 없어 2인 승인을 진행할 수 없습니다.",
                )
            if not request.confirm_token:
                raise HTTPException(
                    status_code=428,
                    detail=("이 작업은 2단계 확인이 필요합니다. 분석 단계에서 발급된 "
                            "confirm_token 을 인증된 두 번째 승인자가 제시해야 합니다."),
                )
            # **서버 발급 토큰과 정확히 일치**해야 한다 — 임의 문자열 차단.
            if not issued or not secrets.compare_digest(
                    (request.confirm_token or "").encode("utf-8"),
                    issued.encode("utf-8")):
                raise HTTPException(
                    status_code=403,
                    detail="confirm_token 이 유효하지 않습니다(서버가 발급한 값이 아님).",
                )
            # 두 신원 모두 서버가 자격증명에서 도출한다. 같은 호출자가 본문에
            # 다른 이름을 써 넣어 2인 확인을 우회할 수 없다.
            if authenticated_actor == requester:
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
    except HTTPException:
        _proposals[request.proposal_id] = proposal   # 실행 전 실패 — 되돌린다
        raise

    # Build the remote command — 사용자 입력은 shlex.quote 로 escape (RCE 차단).
    # 명령 빌드 예외는 **실행 전 실패**이므로 proposal 을 되돌려 재시도
    # 가능하게 한다. SSH dispatch 이후 예외는 실행 여부가 불명확하므로 복원하지
    # 않는다. _ssh_exec 이 반환하면 exit code 와 무관하게 소비된 채로 둔다.
    import re as _re
    import shlex as _shlex
    _NAME_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")

    try:
        if proposal.command_template_id:
            from registry import CommandTemplateRegistry  # type: ignore
            reg = CommandTemplateRegistry()
            remote_cmd = reg.build_command(proposal.command_template_id, proposal.parameters)
        else:
            # Fallback: docker restart — container 이름 strict 화이트리스트 검증 후만.
            container = proposal.target_container or "app"
            if not _NAME_RE.match(container):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid target_container (forbidden characters).",
                )
            remote_cmd = "docker restart " + _shlex.quote(container)
    except HTTPException:
        _proposals[request.proposal_id] = proposal
        raise
    except Exception as exc:
        _proposals[request.proposal_id] = proposal
        raise HTTPException(status_code=422, detail=f"Command build failed: {exc}") from exc

    try:
        exec_result = await _ssh_exec(
            host=ssh_host,
            user=ssh_user,
            key_path=ssh_key,
            command=remote_cmd,
        )
    except _SSHNotDispatchedError as exc:
        # 로컬 SSH 프로세스가 시작되지 않은 확정적 실행 전 실패만 재시도 가능.
        _proposals[request.proposal_id] = proposal
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        # 타임아웃/연결 단절은 원격 명령이 시작된 뒤 발생했을 수도 있다. 이
        # proposal 을 되살리면 비멱등 조치가 재시도 때 두 번 실행될 수 있으므로
        # 소비된 상태로 유지하고 실행 결과를 '불명'으로 보고한다.
        raise HTTPException(
            status_code=502,
            detail=("SSH 실행 결과를 확인할 수 없습니다. proposal 은 중복 실행 "
                    f"방지를 위해 소비되었습니다: {exc}"),
        ) from exc

    return {
        "status": "executed" if exec_result["exit_code"] == 0 else "failed",
        "proposal_id": request.proposal_id,
        **exec_result,
    }
