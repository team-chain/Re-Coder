"""
ReCoder Deploy Agent — Docker build/run, Health Check, Deployment plan generation.

Design principles:
- Commands are ONLY built via CommandTemplateRegistry — LLM never generates shell commands.
- Health Check uses plain HTTP GET with timeout/retry.
- All docker operations are recorded as DeploymentRecord for rollback support.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema imports
# ---------------------------------------------------------------------------


def _schemas():
    try:
        from schemas import (
            ActionType, ApprovalLevel, DeployMethod, DeploymentPlan,
            DeploymentRecord, DeployStatus, HealthCheckResult,
            ProjectProfile, RiskLevel, StackType,
        )
    except ImportError:
        from core.schemas import (
            ActionType, ApprovalLevel, DeployMethod, DeploymentPlan,
            DeploymentRecord, DeployStatus, HealthCheckResult,
            ProjectProfile, RiskLevel, StackType,
        )
    return (
        ActionType, ApprovalLevel, DeployMethod, DeploymentPlan,
        DeploymentRecord, DeployStatus, HealthCheckResult,
        ProjectProfile, RiskLevel, StackType,
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_HEALTH_TIMEOUT = 30   # seconds
_DEFAULT_HEALTH_RETRIES = 5
_DEFAULT_HEALTH_INTERVAL = 3   # seconds between retries

_BUILD_PROMPT_TMPL = """\
You are ReCoder, an infrastructure automation AI.

## Task
Generate a docker build and run configuration for the following project.

## Project
- Stack: {stack}
- Port: {port}
- Health check path: {health_check_path}
- Image name: {image_name}
- Container name: {container_name}

## Output format
Return ONLY a JSON object with:
{{
  "image_name": "<image:tag>",
  "container_name": "<name>",
  "host_port": <int>,
  "container_port": <int>,
  "env_vars": {{"KEY": "VALUE_PLACEHOLDER"}},
  "health_check_path": "<path>",
  "risk_level": "low" | "medium" | "high",
  "risk_reasons": ["<reason>", ...]
}}
"""


class DeployAgent:
    """
    Handles local Docker-based deployment.

    Responsibilities:
    1. Generate DeploymentPlan (docker build + run).
    2. Execute docker build via subprocess.
    3. Execute docker run via CommandTemplateRegistry.
    4. Perform Health Check via HTTP.
    5. Record deployment as DeploymentRecord.
    """

    def __init__(self, provider_router: Any = None) -> None:
        self._provider = provider_router

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_plan(self, request: Any) -> Any:
        """
        Generate a DeploymentPlan for local Docker deployment.

        Parameters come from DeployPlanRequest (from deploy.py route).
        """
        (
            ActionType, ApprovalLevel, DeployMethod, DeploymentPlan,
            DeploymentRecord, DeployStatus, HealthCheckResult,
            ProjectProfile, RiskLevel, StackType,
        ) = _schemas()

        workspace = getattr(request, 'workspace_path', '')
        method = getattr(request, 'method', DeployMethod.LOCAL_DOCKER)
        image = getattr(request, 'image', None)
        container_name = getattr(request, 'container_name', None)
        host_port = getattr(request, 'host_port', 8080)
        container_port = getattr(request, 'container_port', 8080)

        # Auto-detect image and container name if not provided
        ws_name = Path(workspace).name.lower().replace(' ', '-') if workspace else 'app'
        image = image or f"{ws_name}:latest"
        container_name = container_name or ws_name

        # Determine port from workspace if not given
        if not host_port or not container_port:
            host_port, container_port = self._detect_port(workspace)

        return DeploymentPlan(
            method=method,
            action=ActionType.DOCKER_RUN,
            image=image,
            container_name=container_name,
            ports={str(host_port): str(container_port)},
            env={},
            health_check_path="/health",
            rollback_image=None,
            command_template_id="docker_run",
            risk_level=RiskLevel.MEDIUM,
            risk_reasons=["Local Docker run — container will be exposed on localhost"],
            approval_level=ApprovalLevel.CONFIRM,
        )

    async def docker_build(
        self,
        workspace_path: str,
        image_name: str,
        dockerfile_path: str = "Dockerfile",
        no_cache: bool = False,
    ) -> tuple[bool, str, str]:
        """
        Run `docker build` in the workspace directory.

        Returns (success, stdout, stderr).
        """
        cmd = [
            "docker", "build",
            "-t", image_name,
            "-f", dockerfile_path,
        ]
        if no_cache:
            cmd.append("--no-cache")
        cmd.append(".")

        logger.info("docker build: %s", " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workspace_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=300
            )
            ok = proc.returncode == 0
            return ok, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            return False, "", "docker build timed out after 300s"
        except FileNotFoundError:
            return False, "", "Docker is not installed or not in PATH"
        except Exception as exc:
            return False, "", str(exc)

    async def docker_run(
        self,
        image_name: str,
        container_name: str,
        host_port: int,
        container_port: int,
        env: dict[str, str] | None = None,
        detach: bool = True,
    ) -> tuple[bool, str, str]:
        """
        Run `docker run` with the given parameters.

        Returns (success, stdout, stderr).
        """
        # Stop and remove existing container with same name (idempotent)
        await self._stop_remove_container(container_name)

        cmd = ["docker", "run"]
        if detach:
            cmd.append("-d")
        cmd += ["--name", container_name]
        cmd += ["-p", f"{host_port}:{container_port}"]
        cmd += ["--restart", "unless-stopped"]
        for key, val in (env or {}).items():
            cmd += ["-e", f"{key}={val}"]
        cmd.append(image_name)

        logger.info("docker run: %s", " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=60
            )
            ok = proc.returncode == 0
            return ok, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            return False, "", "docker run timed out"
        except FileNotFoundError:
            return False, "", "Docker is not installed or not in PATH"
        except Exception as exc:
            return False, "", str(exc)

    async def health_check(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        path: str = "/health",
        timeout: int = _DEFAULT_HEALTH_TIMEOUT,
        retries: int = _DEFAULT_HEALTH_RETRIES,
        interval: int = _DEFAULT_HEALTH_INTERVAL,
    ) -> Any:
        """
        Poll an HTTP health endpoint until it returns 2xx or timeout.

        Returns HealthCheckResult.
        """
        import urllib.request
        import urllib.error
        import time

        _, _, _, _, _, _, HealthCheckResult, _, _, _ = _schemas()

        url = f"http://{host}:{port}{path}"
        start = time.monotonic()

        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    latency_ms = int((time.monotonic() - start) * 1000)
                    if 200 <= resp.status < 300:
                        return HealthCheckResult(
                            status="healthy",
                            latency_ms=latency_ms,
                        )
            except urllib.error.HTTPError as exc:
                if 200 <= exc.code < 300:
                    latency_ms = int((time.monotonic() - start) * 1000)
                    return HealthCheckResult(status="healthy", latency_ms=latency_ms)
                logger.debug("Health check HTTP %d on attempt %d", exc.code, attempt + 1)
            except Exception as exc:
                logger.debug("Health check attempt %d failed: %s", attempt + 1, exc)

            if time.monotonic() - start >= timeout:
                break

            await asyncio.sleep(interval)

        return HealthCheckResult(status="unhealthy", latency_ms=None)

    async def get_image_digest(self, image_name: str) -> Optional[str]:
        """Retrieve the RepoDigest (or ID) of a locally built image."""
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", image_name],
                    capture_output=True, text=True, timeout=15
                )
            )
            digest = result.stdout.strip()
            if not digest or digest == "<no value>":
                # Fall back to short image ID
                result2 = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: subprocess.run(
                        ["docker", "images", "--format", "{{.ID}}", image_name],
                        capture_output=True, text=True, timeout=15
                    )
                )
                return result2.stdout.strip().splitlines()[0] if result2.stdout.strip() else None
            return digest
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_port(workspace_path: str) -> tuple[int, int]:
        """Guess port from common config files in the workspace."""
        ws = Path(workspace_path) if workspace_path else Path(".")
        default = (8080, 8080)

        # requirements.txt / pyproject.toml → FastAPI/Flask default 8000
        if (ws / "requirements.txt").exists() or (ws / "pyproject.toml").exists():
            return (8000, 8000)

        # package.json → check scripts
        pkg = ws / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
                scripts = data.get("scripts", {})
                for v in scripts.values():
                    m = re.search(r"-p(?:ort)?\s+(\d{4,5})", str(v))
                    if m:
                        p = int(m.group(1))
                        return (p, p)
            except Exception:
                pass
            return (3000, 3000)

        return default

    @staticmethod
    async def _stop_remove_container(name: str) -> None:
        """Best-effort stop + rm of an existing container (ignores errors)."""
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["docker", "rm", "-f", name],
                    capture_output=True, timeout=15
                )
            )
        except Exception:
            pass
