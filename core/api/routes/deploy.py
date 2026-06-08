"""
ReCoder Core — Deployment Routes

Handles Dockerfile/infra file generation, security scans, deployment
planning, execution, records, and rollback.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from schemas import (
    ApprovalLevel,
    DeployMethod,
    DeploymentPlan,
    DeploymentRecord,
    DeployStatus,
    FileType,
    InfraFileProposal,
    RiskLevel,
    StackType,
)

router = APIRouter(tags=["deploy"])

# ---------------------------------------------------------------------------
# In-process stores (per server lifetime)
# ---------------------------------------------------------------------------

_infra_proposals: dict[str, InfraFileProposal] = {}
_deployment_plans: dict[str, DeploymentPlan] = {}
_deployment_records: dict[str, DeploymentRecord] = {}

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class DockerfileRequest(BaseModel):
    workspace_path: str
    stack: Optional[StackType] = None
    project_id: Optional[str] = None
    extra_context: Optional[str] = None


class ScanRequest(BaseModel):
    workspace_path: str
    scan_type: str  # "trivy" | "hadolint" | "gitleaks"
    target_path: Optional[str] = None


class DeployPlanRequest(BaseModel):
    workspace_path: str
    project_id: Optional[str] = None
    method: DeployMethod = DeployMethod.LOCAL_DOCKER
    image: Optional[str] = None
    container_name: Optional[str] = None
    host_port: Optional[int] = None
    container_port: Optional[int] = None
    extra_context: Optional[str] = None
    # Security gate (Ship Stage). Default False — security scans always run.
    # Set to True to bypass Trivy/Hadolint pre-deployment gating (e.g. CI dry runs).
    skip_security_scan: bool = False
    # 설계 §4.6 / §34 — 배포 직후 5분 헬스 체크 자동 트리거 여부.
    enable_continuous_verification: bool = True


class ExecuteRequest(BaseModel):
    plan_id: str
    approved: bool
    # Per-execute override; if None, falls back to the plan's setting.
    enable_continuous_verification: Optional[bool] = None


class RollbackRequest(BaseModel):
    deployment_id: str


# ---------------------------------------------------------------------------
# Agent singletons — initialised lazily on first use so that missing
# optional dependencies (boto3, google-generativeai) only raise at call
# time rather than blocking the whole server startup.
# ---------------------------------------------------------------------------

_infra_agent = None   # type: ignore
_deploy_agent = None  # type: ignore


def _get_infra_agent():
    global _infra_agent
    if _infra_agent is None:
        try:
            from llm.provider_router import LLMProviderRouter  # type: ignore
            from registry import FileTemplateRegistry  # type: ignore
            from agents.infra_agent import InfraAgent  # type: ignore
            _infra_agent = InfraAgent(
                provider_router=LLMProviderRouter(),
                file_template_registry=FileTemplateRegistry(),
            )
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("InfraAgent unavailable: %s", exc)
    return _infra_agent


def _get_deploy_agent():
    global _deploy_agent
    if _deploy_agent is None:
        try:
            from llm.provider_router import LLMProviderRouter  # type: ignore
            from agents.deploy_agent import DeployAgent  # type: ignore
            _deploy_agent = DeployAgent(
                provider_router=LLMProviderRouter(),
            )
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("DeployAgent unavailable: %s", exc)
    return _deploy_agent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_stack(workspace_path: str) -> StackType:
    """Heuristically detect the project stack from workspace files."""
    ws = Path(workspace_path)
    if (ws / "requirements.txt").exists() or (ws / "pyproject.toml").exists():
        # Limit to 20 files to avoid blocking the async event loop on large projects.
        for f in list(ws.rglob("*.py"))[:20]:
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
        return StackType.PYTHON_FASTAPI  # Default for Python
    if (ws / "package.json").exists():
        try:
            import json
            pkg = json.loads((ws / "package.json").read_text(encoding="utf-8"))
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


def _log_scan_to_session(scan_type: str, target: str, result: dict) -> None:
    """Best-effort persistence of scan outcome to SQLite session_logger.

    Failures are swallowed so the API path is never broken by logger problems.
    """
    try:
        from datetime import datetime, timezone
        from session_logger import get_session_logger  # type: ignore
        from schemas import SessionEvent  # type: ignore

        crit = int(result.get("critical_count", 0))
        high = int(result.get("high_count", 0))
        summary = result.get("summary") or f"{scan_type} scan on {target}"
        status = result.get("status", "unknown")

        event = SessionEvent(
            time=datetime.now(timezone.utc).isoformat(),
            event_type=f"security_scan.{scan_type}",
            error_summary=f"crit={crit} high={high}",
            error_fingerprint=f"{scan_type}:{target}",
            related_file_names=[target] if target else [],
            ai_suggestion_summary=str(summary)[:500],
            user_action="ignored",
            result="success" if status == "ok" else ("failed" if status == "error" else "pending"),
            validation="unknown",
        )
        get_session_logger().log_event("ship-stage", event)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).debug("session_logger unavailable for scan log: %s", exc)


def _normalise_scan_result(scan_type: str, target: str, raw: dict) -> dict:
    """Map an InfraAgent scan output dict into the canonical ScanResult shape.

    ScanResult = {status, scan_type, critical_count, high_count, medium_count,
                  findings[], summary, target}
    """
    if not raw.get("success", False):
        return {
            "status": "error",
            "scan_type": scan_type,
            "target": target,
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "findings": [],
            "summary": raw.get("error") or raw.get("summary") or "Scan failed.",
            "message": raw.get("error", "Scan failed."),
        }

    findings: list[dict] = []
    critical_count = 0
    high_count = 0
    medium_count = 0

    if scan_type == "trivy":
        critical = raw.get("critical", []) or []
        high = raw.get("high", []) or []
        critical_count = len(critical)
        high_count = len(high)
        for entry in critical:
            findings.append({"severity": "CRITICAL", **entry})
        for entry in high:
            findings.append({"severity": "HIGH", **entry})
    elif scan_type == "hadolint":
        for v in raw.get("violations", []) or []:
            level = str(v.get("level", "")).lower()
            if level == "error":
                critical_count += 1
                severity = "CRITICAL"
            elif level == "warning":
                high_count += 1
                severity = "HIGH"
            else:
                medium_count += 1
                severity = "MEDIUM"
            findings.append({"severity": severity, **v})
    elif scan_type == "gitleaks":
        # Every leaked secret is treated as CRITICAL.
        for f in raw.get("findings", []) or []:
            critical_count += 1
            findings.append({"severity": "CRITICAL", **f})

    return {
        "status": "ok",
        "scan_type": scan_type,
        "target": target,
        "critical_count": critical_count,
        "high_count": high_count,
        "medium_count": medium_count,
        "findings": findings,
        "summary": raw.get("summary") or "",
    }


async def _execute_scan(scan_type: str, workspace_path: str, target_path: Optional[str]) -> dict:
    """Dispatch to InfraAgent.run_{trivy,hadolint,gitleaks}_scan and normalise output.

    Dispatch rules per scan_type:
      - trivy:    target = target_path (image name, e.g. "myapp:latest")
                  — if missing, falls back to "<basename(workspace)>:latest".
      - hadolint: target = target_path (Dockerfile path)
                  — if missing, falls back to "<workspace>/Dockerfile".
      - gitleaks: target = workspace_path (the repo root).
    """
    if scan_type not in {"trivy", "hadolint", "gitleaks"}:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scan type: '{scan_type}'. Use trivy, hadolint, or gitleaks.",
        )

    agent = _get_infra_agent()
    if agent is None:
        return {
            "status": "error",
            "scan_type": scan_type,
            "target": target_path or workspace_path,
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "findings": [],
            "summary": "InfraAgent unavailable (dependencies missing).",
            "message": "InfraAgent unavailable on this host.",
        }

    ws = Path(workspace_path) if workspace_path else None

    try:
        if scan_type == "trivy":
            image = target_path
            if not image:
                if ws is not None and ws.exists():
                    image = f"{ws.name.lower().replace(' ', '-') or 'app'}:latest"
                else:
                    image = "app:latest"
            raw = await asyncio.wait_for(agent.run_trivy_scan(image), timeout=300)
            target_for_log = image
        elif scan_type == "hadolint":
            dockerfile = target_path
            if not dockerfile and ws is not None:
                dockerfile = str(ws / "Dockerfile")
            if not dockerfile:
                raise HTTPException(
                    status_code=400,
                    detail="hadolint scan requires either target_path or workspace_path.",
                )
            raw = await asyncio.wait_for(agent.run_hadolint_scan(dockerfile), timeout=300)
            target_for_log = dockerfile
        else:  # gitleaks
            scan_root = workspace_path or target_path
            if not scan_root:
                raise HTTPException(
                    status_code=400,
                    detail="gitleaks scan requires workspace_path.",
                )
            raw = await asyncio.wait_for(agent.run_gitleaks_scan(scan_root), timeout=300)
            target_for_log = scan_root
    except asyncio.TimeoutError:
        result = {
            "status": "error",
            "scan_type": scan_type,
            "target": target_path or workspace_path,
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "findings": [],
            "summary": f"Scan '{scan_type}' exceeded 300s timeout.",
            "message": "timeout",
        }
        _log_scan_to_session(scan_type, target_path or workspace_path or "", result)
        return result
    except FileNotFoundError:
        result = {
            "status": "error",
            "scan_type": scan_type,
            "target": target_path or workspace_path,
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "findings": [],
            "summary": "Docker is not available on this host. Start Docker Desktop and retry.",
            "message": "docker_not_found",
        }
        _log_scan_to_session(scan_type, target_path or workspace_path or "", result)
        return result
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        result = {
            "status": "error",
            "scan_type": scan_type,
            "target": target_path or workspace_path,
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "findings": [],
            "summary": f"Scan failed: {exc}",
            "message": str(exc),
        }
        _log_scan_to_session(scan_type, target_path or workspace_path or "", result)
        return result

    normalised = _normalise_scan_result(scan_type, target_for_log, raw)
    _log_scan_to_session(scan_type, target_for_log, normalised)
    return normalised


def _write_proposal_to_workspace(proposal, workspace_override, proposal_id):
    """Proposal 파일을 워크스페이스 루트 기준으로 디스크에 쓴다.

    상대 target_path 면 (override -> proposal.workspace_path -> cwd) 순으로 루트 결정.
    과거 버그: 루트를 항상 Path.cwd()(=Core 실행 디렉토리 core/)로 잡아
    .github/workflows/deploy.yml 이 사용자 프로젝트가 아니라 core/ 에 써졌다.
    """
    target = Path(proposal.target_path)
    if not target.is_absolute():
        root = (
            workspace_override
            or getattr(proposal, "workspace_path", None)
            or str(Path.cwd())
        )
        target = Path(root).expanduser().resolve() / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(proposal.content, encoding="utf-8")
    return {"status": "saved", "proposal_id": proposal_id, "path": str(target)}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/deploy/dockerfile")
async def generate_dockerfile(request: DockerfileRequest) -> InfraFileProposal:
    """
    Generate a Dockerfile proposal for the workspace.

    Auto-detects the stack if not specified, then delegates to the
    InfraAgent (or returns a template-based placeholder).
    """
    stack = request.stack or _detect_stack(request.workspace_path)

    agent = _get_infra_agent()
    if agent is not None:
        # InfraAgent.generate_dockerfile(workspace_path, project: ProjectProfile)
        # 시그니처에 맞춰 ProjectProfile 을 구성. 사용자가 /api/project/scan 을 안 했어도
        # 최소한의 정보로 호출 가능하도록 inline 구성.
        try:
            from project_scanner import get_project_scanner  # type: ignore
            project = get_project_scanner().scan(request.workspace_path)
        except Exception:
            # 폴백 — 빈 ProjectProfile (필수 필드만)
            from schemas import ProjectProfile  # type: ignore
            project = ProjectProfile(
                workspace_path=request.workspace_path,
                stack=stack,
            )
        try:
            proposal = await agent.generate_dockerfile(request.workspace_path, project)
        except TypeError:
            # 구버전 호환 — 일부 InfraAgent 구현은 시그니처가 다를 수 있음
            proposal = await agent.generate_dockerfile(request.workspace_path)  # type: ignore[call-arg]
    else:
        # Placeholder: load from FileTemplateRegistry
        from registry import FileTemplateRegistry  # type: ignore
        reg = FileTemplateRegistry()
        template_id = f"Dockerfile.{stack.value}"
        try:
            content = reg.render(template_id, {"WORKSPACE": request.workspace_path})
        except Exception:
            content = f"# Auto-generated Dockerfile for {stack.value}\nFROM python:3.11-slim\nWORKDIR /app\nCOPY . .\n"

        proposal = InfraFileProposal(
            file_type=FileType.DOCKERFILE,
            target_path="Dockerfile",
            content=content,
            base_template=template_id,
            risk_level=RiskLevel.LOW,
            approval_level=ApprovalLevel.CONFIRM,
        )

    if not getattr(proposal, "workspace_path", None):
        proposal = proposal.model_copy(update={
            "workspace_path": str(Path(request.workspace_path).expanduser().resolve()),
        })
    _infra_proposals[proposal.proposal_id] = proposal
    return proposal


@router.post("/api/deploy/dockerfile/approve")
async def approve_dockerfile(proposal_id: str, approved: bool, workspace_path: str = "") -> dict:
    """Approve or reject a Dockerfile / infra file proposal.

    워크스페이스 루트 기준으로 쓴다 (cwd 폴백 버그 동일 수정).
    """
    proposal = _infra_proposals.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Proposal '{proposal_id}' not found.")

    if not approved:
        del _infra_proposals[proposal_id]
        return {"status": "rejected", "proposal_id": proposal_id}

    result = _write_proposal_to_workspace(proposal, workspace_path, proposal_id)
    del _infra_proposals[proposal_id]
    return result


# ---------------------------------------------------------------------------
# GitHub Actions Workflow generation (설계 §4.1.2 Ship Stage 확장)
# ---------------------------------------------------------------------------


class GithubActionsRequest(BaseModel):
    workspace_path: str
    project_id: Optional[str] = None
    extra_context: Optional[str] = None


@router.post("/api/deploy/github-actions")
async def generate_github_actions_route(request: GithubActionsRequest) -> InfraFileProposal:
    """
    Generate a GitHub Actions workflow YAML for the workspace.

    Returns an InfraFileProposal (file_type=GITHUB_ACTIONS) targeting
    `.github/workflows/deploy.yml`. The proposal is held in-memory until
    the user approves via `/api/deploy/github-actions/approve`.

    설계 A.4 InfraFileProposal — file_type "github-actions" 케이스.
    """
    # Lazy import: project_scanner / infra_agent 둘 다 server.py 전역에 의존하지 않도록
    try:
        from project_scanner import get_project_scanner  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"project_scanner import 실패: {e}") from e

    try:
        from infra_agent import generate_github_actions  # type: ignore
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"infra_agent.generate_github_actions import 실패: {e}",
        ) from e

    # 1. 워크스페이스 스캔 → ProjectProfile
    ws_path = request.workspace_path
    if not ws_path:
        raise HTTPException(status_code=400, detail="workspace_path 가 비어있습니다.")
    if not Path(ws_path).expanduser().resolve().exists():
        raise HTTPException(status_code=404, detail=f"워크스페이스 경로가 없습니다: {ws_path}")

    try:
        scanner = get_project_scanner()
        project = await asyncio.to_thread(scanner.scan, ws_path)
    except Exception as e:
        # 스캔 실패(스택 미감지/검증 오류 등)해도 워크플로 생성은 계속한다.
        # generate_github_actions 가 project=None 이면 자체 폴백(generic CI)으로 생성.
        import logging as _logging
        _logging.getLogger(__name__).warning("프로젝트 스캔 실패, generic 폴백: %s", e)
        project = None

    # 2. 워크플로우 생성 (FileTemplate Registry 기반)
    try:
        proposal = await asyncio.to_thread(generate_github_actions, project, ws_path)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"GitHub Actions 워크플로우 생성 실패: {e}",
        ) from e

    # 3. file_type 보정 + workspace_path 절대경로 박기.
    #    approve 단계가 이 경로 기준으로 .github/workflows/deploy.yml 을 쓰지 않으면
    #    Core 의 cwd(=core/)에 써지는 버그가 있었다.
    abs_ws = str(Path(ws_path).expanduser().resolve())
    update = {"workspace_path": abs_ws}
    if proposal.file_type != FileType.GITHUB_ACTIONS:
        update["file_type"] = FileType.GITHUB_ACTIONS
    proposal = proposal.model_copy(update=update)

    _infra_proposals[proposal.proposal_id] = proposal
    return proposal


@router.post("/api/deploy/github-actions/approve")
async def approve_github_actions(proposal_id: str, approved: bool, workspace_path: str = "") -> dict:
    """
    Approve or reject a GitHub Actions workflow proposal.

    On approve, writes the YAML to `<workspace>/.github/workflows/deploy.yml`.
    파일 위치 우선순위(상대 target_path): 쿼리 workspace_path -> proposal.workspace_path
    -> (폴백) cwd. 1·2 가 항상 채워지므로 더는 core/ 에 잘못 써지지 않는다.
    """
    proposal = _infra_proposals.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Proposal '{proposal_id}' not found.")

    if not approved:
        del _infra_proposals[proposal_id]
        return {"status": "rejected", "proposal_id": proposal_id}

    result = _write_proposal_to_workspace(proposal, workspace_path, proposal_id)
    del _infra_proposals[proposal_id]
    return result


@router.post("/api/deploy/scan")
async def run_scan(request: ScanRequest) -> dict:
    """
    Run a one-shot security scan via the InfraAgent.

    Supported scan_type values: ``trivy``, ``hadolint``, ``gitleaks``.

    Returns a normalised ScanResult dict:
      {
        status: "ok" | "error",
        scan_type: str,
        target: str,
        critical_count: int,
        high_count: int,
        medium_count: int,
        findings: list[dict],
        summary: str,
      }

    Docker not running, missing dependencies, and timeouts return
    ``{status: "error", message: "..."}``. Only invalid ``scan_type`` raises
    HTTP 400.
    """
    return await _execute_scan(
        request.scan_type, request.workspace_path, request.target_path
    )


async def _run_pre_deploy_security_gate(request: DeployPlanRequest) -> dict:
    """Run Trivy (filesystem/image) + Hadolint (Dockerfile) before planning.

    Returns a dict with:
      - blockers:    list[str]  — human-readable critical findings
      - risk_reasons:list[str]  — additional non-critical reasons
      - elevated:    bool       — True if any critical was found
      - reports:     dict       — raw normalised scan reports per scanner
    """
    blockers: list[str] = []
    risk_reasons: list[str] = []
    reports: dict = {}

    workspace = request.workspace_path
    ws_path = Path(workspace) if workspace else None
    dockerfile_path = None
    if ws_path is not None and (ws_path / "Dockerfile").exists():
        dockerfile_path = str(ws_path / "Dockerfile")

    # Hadolint — only meaningful if a Dockerfile exists.
    if dockerfile_path is not None:
        hadolint_report = await _execute_scan("hadolint", workspace, dockerfile_path)
        reports["hadolint"] = hadolint_report
        if hadolint_report.get("status") == "ok":
            crit = int(hadolint_report.get("critical_count", 0))
            if crit > 0:
                blockers.append(f"Hadolint: {crit} Dockerfile error(s)")
            high = int(hadolint_report.get("high_count", 0))
            if high > 0:
                risk_reasons.append(f"Hadolint: {high} warning(s)")

    # Trivy — only against an explicitly-provided image name.
    if request.image:
        trivy_report = await _execute_scan("trivy", workspace, request.image)
        reports["trivy"] = trivy_report
        if trivy_report.get("status") == "ok":
            crit = int(trivy_report.get("critical_count", 0))
            if crit > 0:
                blockers.append(f"Trivy: {crit} CRITICAL CVE(s) in {request.image}")
            high = int(trivy_report.get("high_count", 0))
            if high > 0:
                risk_reasons.append(f"Trivy: {high} HIGH CVE(s) in {request.image}")

    return {
        "blockers": blockers,
        "risk_reasons": risk_reasons,
        "elevated": bool(blockers),
        "reports": reports,
    }


@router.post("/api/deploy/plan")
async def create_deployment_plan(request: DeployPlanRequest) -> DeploymentPlan:
    """Generate an executable DeploymentPlan via the DeployAgent.

    Ship Stage security gate: unless ``skip_security_scan=true``, Trivy and
    Hadolint are run before the plan is built. If any CRITICAL findings are
    detected, the resulting plan is annotated with ``risk_level=HIGH``,
    ``approval_level=DOUBLE_CONFIRM``, and the blockers are embedded into
    ``risk_reasons`` (prefixed with ``BLOCKER:``).
    """
    gate: dict = {"blockers": [], "risk_reasons": [], "elevated": False, "reports": {}}
    if not request.skip_security_scan:
        try:
            gate = await _run_pre_deploy_security_gate(request)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("Pre-deploy security gate failed: %s", exc)
            gate = {
                "blockers": [],
                "risk_reasons": [f"Security gate did not run: {exc}"],
                "elevated": False,
                "reports": {},
            }

    deploy_agent = _get_deploy_agent()
    if deploy_agent is not None:
        plan = await deploy_agent.create_plan(request)
    else:
        # Placeholder plan
        plan = DeploymentPlan(
            method=request.method,
            action=__import__("schemas", fromlist=["ActionType"]).ActionType.DOCKER_RUN,
            image=request.image or "app:latest",
            container_name=request.container_name or "app",
            ports={str(request.host_port or 8080): str(request.container_port or 8080)},
            health_check_path="/health",
            risk_level=RiskLevel.MEDIUM,
            approval_level=ApprovalLevel.CONFIRM,
        )

    # Apply the security-gate verdict onto the plan.
    extra_reasons: list[str] = []
    extra_reasons.extend(f"BLOCKER: {b}" for b in gate["blockers"])
    extra_reasons.extend(gate["risk_reasons"])
    if extra_reasons:
        plan.risk_reasons = list(plan.risk_reasons) + extra_reasons
    if gate["elevated"]:
        plan.risk_level = RiskLevel.HIGH
        plan.approval_level = ApprovalLevel.DOUBLE_CONFIRM

    _deployment_plans[plan.plan_id] = plan
    return plan


@router.post("/api/deploy/execute")
async def execute_deployment(request: ExecuteRequest) -> dict:
    """
    Execute an approved DeploymentPlan.

    Uses the CommandTemplateRegistry to build shell commands and runs them.
    """
    plan = _deployment_plans.get(request.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan '{request.plan_id}' not found.")

    if not request.approved:
        del _deployment_plans[request.plan_id]
        return {"status": "cancelled", "plan_id": request.plan_id}

    # ── 보안: image / container_name 화이트리스트 검증 (shell injection 차단) ──
    # docker 이미지 이름 문법: [registry/][namespace/]name[:tag][@digest]
    # 컨테이너 이름: [a-zA-Z0-9][a-zA-Z0-9_.-]+
    import re as _re
    _IMG_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/\-@]{0,254}$")
    _NAME_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")
    if plan.image and not _IMG_RE.match(plan.image):
        raise HTTPException(status_code=400, detail="Invalid image name (forbidden characters).")
    if plan.container_name and not _NAME_RE.match(plan.container_name):
        raise HTTPException(status_code=400, detail="Invalid container name (forbidden characters).")
    for _hp, _cp in plan.ports.items():
        if not str(_hp).isdigit() or not str(_cp).isdigit():
            raise HTTPException(status_code=400, detail="Port must be numeric.")

    if plan.command_template_id:
        # Template-based path: registry 가 list-form 을 반환하도록 요구하고,
        # str 반환 시 shlex.split 으로 안전하게 토큰화한다 (shell=False 보장).
        from registry import CommandTemplateRegistry  # type: ignore
        import shlex as _shlex
        reg = CommandTemplateRegistry()
        try:
            first_hp, first_cp = next(iter(plan.ports.items()), ("8080", "8080"))
            built = reg.build_command(plan.command_template_id, {
                "image_name": plan.image or "",
                "container_name": plan.container_name or "",
                "host_port": int(first_hp),
                "container_port": int(first_cp),
            })
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Command build failed: {exc}") from exc
        if isinstance(built, list):
            cmd_args = [str(x) for x in built]
        else:
            # str 반환은 deprecated — 시연 호환을 위해 shlex 로 토큰화 (shell 호출 없음)
            cmd_args = _shlex.split(str(built))
    else:
        # 안전한 args list 직접 조립 (shell=False 강제)
        cmd_args: list[str] = ["docker", "run", "-d", "--name", str(plan.container_name)]
        for _hp, _cp in plan.ports.items():
            cmd_args.extend(["-p", f"{int(_hp)}:{int(_cp)}"])
        cmd_args.extend(["--restart", "unless-stopped", str(plan.image)])

    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                cmd_args, shell=False, capture_output=True, text=True, timeout=300
            ),
        )
        success = result.returncode == 0
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Deployment execution failed: {exc}") from exc

    # Record the deployment
    from schemas import ActionType
    record = DeploymentRecord(
        project_id=getattr(plan, "project_id", "unknown"),
        method=plan.method,
        image=plan.image or "",
        container_name=plan.container_name or "",
        health_check_path=plan.health_check_path,
        rollback_target=plan.rollback_image,
        status=DeployStatus.SUCCESS if success else DeployStatus.FAILED,
    )
    _deployment_records[record.deployment_id] = record
    del _deployment_plans[request.plan_id]

    # 설계 §4.6 / §34 — 배포 성공 직후 Continuous Verification 자동 트리거.
    # 실패 시에도 verification 자체의 예외가 배포 응답을 흔들지 않도록 모두 catch.
    cv_started = False
    cv_enabled = request.enable_continuous_verification
    if cv_enabled is None:
        cv_enabled = bool(getattr(plan, "enable_continuous_verification", True))
    if success and cv_enabled:
        get_continuous_verifier = None  # type: ignore
        try:
            from preflight.continuous_verification import get_continuous_verifier  # type: ignore
        except Exception:  # noqa: BLE001
            try:
                from core.preflight.continuous_verification import get_continuous_verifier  # type: ignore
            except Exception as _exc:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(
                    "ContinuousVerifier unavailable: %s", _exc,
                )

        if get_continuous_verifier is not None:
            try:
                first_hp = next(iter(plan.ports.items()), ("8080", "8080"))[0]
                health_path = plan.health_check_path or "/health"
                if not health_path.startswith("/"):
                    health_path = "/" + health_path
                health_url = f"http://localhost:{first_hp}{health_path}"
                verifier = get_continuous_verifier()
                await verifier.start(
                    deployment_id=record.deployment_id,
                    container_name=record.container_name,
                    health_check_url=health_url,
                    duration_minutes=5,
                    project_id=getattr(plan, "project_id", None),
                )
                cv_started = True
            except Exception as _exc:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(
                    "Continuous verification start failed (deployment still success): %s",
                    _exc,
                )

    return {
        "status": "success" if success else "failed",
        "deployment_id": record.deployment_id,
        "stdout": result.stdout[:2000],
        "stderr": result.stderr[:2000],
        "continuous_verification": {
            "enabled": bool(cv_enabled),
            "started": bool(cv_started),
        },
    }


@router.post("/api/deploy/local")
async def execute_deployment_local(request: ExecuteRequest) -> dict:
    """
    Alias for /api/deploy/execute — matches the path name specified in
    the v6.4 design document (§20.5 DeploymentPlan, method=local_docker).
    """
    return await execute_deployment(request)


@router.get("/api/deploy/records")
async def list_deployment_records() -> list[DeploymentRecord]:
    """Return all deployment records for the current session."""
    return list(_deployment_records.values())


@router.post("/api/deploy/rollback")
async def rollback(request: RollbackRequest) -> dict:
    """Roll back to the previous image tag for a given deployment."""
    record = _deployment_records.get(request.deployment_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Deployment '{request.deployment_id}' not found.")

    if not record.rollback_target:
        raise HTTPException(
            status_code=422,
            detail=f"Deployment '{request.deployment_id}' has no rollback target.",
        )

    # 보안: container_name / rollback_target 화이트리스트 검증 후 args list 로 호출.
    # shell=True 가 아니므로 && 체이닝 불가 → 3단계 순차 실행.
    import re as _re
    _IMG_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/\-@]{0,254}$")
    _NAME_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")
    if not _NAME_RE.match(record.container_name or ""):
        raise HTTPException(status_code=400, detail="Invalid container_name on record.")
    if not _IMG_RE.match(record.rollback_target):
        raise HTTPException(status_code=400, detail="Invalid rollback_target on record.")

    try:
        loop = asyncio.get_running_loop()
        # 1) docker stop (실패 무시 — 이미 중지됐을 수 있음)
        await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["docker", "stop", record.container_name],
                shell=False, capture_output=True, text=True, timeout=60,
            ),
        )
        # 2) docker rm (실패 무시)
        await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["docker", "rm", record.container_name],
                shell=False, capture_output=True, text=True, timeout=60,
            ),
        )
        # 3) docker run — 이게 실패하면 rollback 실패
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["docker", "run", "-d", "--name", record.container_name,
                 "--restart", "unless-stopped", record.rollback_target],
                shell=False, capture_output=True, text=True, timeout=120,
            ),
        )
        success = result.returncode == 0
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Rollback failed: {exc}") from exc

    record.status = DeployStatus.ROLLED_BACK if success else DeployStatus.FAILED

    return {
        "status": "ok" if success else "failed",
        "deployment_id": request.deployment_id,
        "rolled_back_to": record.rollback_target,
        "stdout": result.stdout[:2000],
        "stderr": result.stderr[:2000],
    }


def _get_verifier():
    """ContinuousVerifier singleton."""
    try:
        from preflight.continuous_verification import get_continuous_verifier
        return get_continuous_verifier()
    except Exception:
        try:
            from core.preflight.continuous_verification import get_continuous_verifier
            return get_continuous_verifier()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"ContinuousVerifier unavailable: {exc}",
            ) from exc


@router.get("/api/deploy/verification/{deployment_id}/status")
async def get_verification_status(deployment_id: str) -> dict:
    verifier = _get_verifier()
    snapshot = verifier.get_status(deployment_id)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"No continuous verification found for deployment '{deployment_id}'.",
        )
    return snapshot


@router.post("/api/deploy/verification/{deployment_id}/stop")
async def stop_verification(deployment_id: str) -> dict:
    verifier = _get_verifier()
    return await verifier.stop(deployment_id)
