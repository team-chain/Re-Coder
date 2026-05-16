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


class ExecuteRequest(BaseModel):
    plan_id: str
    approved: bool


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


async def _run_scan_container(scan_type: str, workspace_path: str, target_path: Optional[str]) -> dict:
    """Run a one-shot security scanner container and return the result."""
    ws = Path(workspace_path)
    scan_configs = {
        "trivy": {
            "image": "aquasec/trivy:latest",
            "cmd": ["trivy", "fs", "--format", "json", "/workspace"],
        },
        "hadolint": {
            "image": "hadolint/hadolint:latest",
            "cmd": ["hadolint", "--format", "json", "/workspace/Dockerfile"],
        },
        "gitleaks": {
            "image": "zricethezav/gitleaks:latest",
            "cmd": ["gitleaks", "detect", "--source", "/workspace", "--report-format", "json"],
        },
    }
    if scan_type not in scan_configs:
        raise HTTPException(status_code=400, detail=f"Unknown scan type: '{scan_type}'. Use trivy, hadolint, or gitleaks.")

    cfg = scan_configs[scan_type]
    mount_target = str(ws) if target_path is None else str(Path(target_path))
    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{mount_target}:/workspace:ro",
        cfg["image"],
        *cfg["cmd"],
    ]

    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=120,
            ),
        )
        import json
        try:
            output = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            output = {"raw_output": result.stdout}

        return {
            "scan_type": scan_type,
            "exit_code": result.returncode,
            "findings": output,
            "stderr": result.stderr[:2000] if result.stderr else None,
        }
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Scan '{scan_type}' timed out.")
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Docker is not available on this host.")


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
        proposal = await agent.generate_dockerfile(request.workspace_path, stack, request.extra_context)
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

    _infra_proposals[proposal.proposal_id] = proposal
    return proposal


@router.post("/api/deploy/dockerfile/approve")
async def approve_dockerfile(proposal_id: str, approved: bool) -> dict:
    """Approve or reject a Dockerfile / infra file proposal."""
    proposal = _infra_proposals.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Proposal '{proposal_id}' not found.")

    if not approved:
        del _infra_proposals[proposal_id]
        return {"status": "rejected", "proposal_id": proposal_id}

    # Write the file to disk
    target = Path(proposal.target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(proposal.content, encoding="utf-8")
    del _infra_proposals[proposal_id]

    return {"status": "saved", "proposal_id": proposal_id, "path": str(target)}


@router.post("/api/deploy/scan")
async def run_scan(request: ScanRequest) -> dict:
    """
    Run a one-shot security scan via a Docker container.

    Supported scan_type values: ``trivy``, ``hadolint``, ``gitleaks``.
    """
    return await _run_scan_container(
        request.scan_type, request.workspace_path, request.target_path
    )


@router.post("/api/deploy/plan")
async def create_deployment_plan(request: DeployPlanRequest) -> DeploymentPlan:
    """Generate an executable DeploymentPlan via the DeployAgent."""
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

    if plan.command_template_id:
        from registry import CommandTemplateRegistry  # type: ignore
        reg = CommandTemplateRegistry()
        try:
            # Use the first port mapping as host_port/container_port for templates.
            # (Templates address a single port; multi-port deployments should
            # use docker-compose, not the template registry.)
            first_hp, first_cp = next(iter(plan.ports.items()), ("8080", "8080"))
            cmd_str = reg.build_command(plan.command_template_id, {
                "image_name": plan.image or "",
                "container_name": plan.container_name or "",
                "host_port": int(first_hp),
                "container_port": int(first_cp),
            })
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Command build failed: {exc}") from exc
    else:
        # Build a direct docker run command
        port_args = " ".join(f"-p {hp}:{cp}" for hp, cp in plan.ports.items())
        cmd_str = (
            f"docker run -d --name {plan.container_name} {port_args} "
            f"--restart unless-stopped {plan.image}"
        )

    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                cmd_str, shell=True, capture_output=True, text=True, timeout=300
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

    return {
        "status": "success" if success else "failed",
        "deployment_id": record.deployment_id,
        "stdout": result.stdout[:2000],
        "stderr": result.stderr[:2000],
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
    """
    Roll back to the previous image tag for a given deployment.

    Reads the rollback_target from the DeploymentRecord and re-runs the
    container with the previous image.
    """
    record = _deployment_records.get(request.deployment_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Deployment '{request.deployment_id}' not found.")

    if not record.rollback_target:
        raise HTTPException(
            status_code=422,
            detail=f"Deployment '{request.deployment_id}' has no rollback target.",
        )

    rollback_cmd = (
        f"docker stop {record.container_name} && "
        f"docker rm {record.container_name} && "
        f"docker run -d --name {record.container_name} "
        f"--restart unless-stopped {record.rollback_target}"
    )

    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                rollback_cmd, shell=True, capture_output=True, text=True, timeout=120
            ),
        )
        success = result.returncode == 0
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Rollback failed: {exc}") from exc

    # Update record status
    record.status = DeployStatus.ROLLED_BACK if success else DeployStatus.FAILED

    return {
        "status": "rolled_back" if success else "failed",
        "deployment_id": request.deployment_id,
        "rollback_image": record.rollback_target,
        "stdout": result.stdout[:2000],
        "stderr": result.stderr[:2000],
    }
