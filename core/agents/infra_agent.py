"""
ReCoder Infra Agent — Dockerfile generation, docker-compose, and security scanning.

Design principles:
- LLM only proposes customisation points; actual file assembly is done by
  FileTemplateRegistry.
- Security scan results from gitleaks never expose secret plaintext to the LLM.
- All docker tool invocations are one-shot ephemeral containers (--rm).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from schemas import (
    ApprovalLevel,
    DeployMethod,
    FileType,
    InfraFileProposal,
    ProjectProfile,
    RiskLevel,
    StackType,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_DOCKERFILE_CUSTOMISE_PROMPT = """\
You are ReCoder, an infrastructure automation AI.

## Task
Analyse the project below and identify the customisation values needed to adapt
the base Dockerfile template for this specific project.

## Project details
- Stack: {stack}
- Package manager: {package_manager}
- Default port: {port}
- Run command: {run_command}
- Health check path: {health_check_path}

## Base template content
```
{template_content}
```

## Files found in workspace
{workspace_summary}

## Output format
Return ONLY a JSON object with string key-value pairs for template customisation.
Keys must match the {{PLACEHOLDER}} markers in the template.

Example:
{{
  "APP_PORT": "8000",
  "START_COMMAND": "uvicorn main:app --host 0.0.0.0 --port 8000",
  "PYTHON_VERSION": "3.11"
}}
"""

_COMPOSE_PROMPT = """\
You are ReCoder, an infrastructure automation AI.

## Task
Generate a minimal docker-compose.yml for the project below.

## Project details
- Stack: {stack}
- Port: {port}
- Dockerfile path: {dockerfile_path}
- Health check path: {health_check_path}
- Required env vars (names only, no values): {env_vars}

Return ONLY a valid docker-compose.yml YAML string.
Do not include any explanation or markdown fences.
"""

_SCAN_SUMMARY_PROMPT = """\
You are a security engineer summarising a container / code scan result.

Scan type: {scan_type}

Raw findings (sanitised — no secret values):
```json
{findings}
```

Return a JSON object with:
{{
  "threat_summary": "<2-3 sentence overview>",
  "critical_count": <int>,
  "high_count": <int>,
  "recommended_actions": ["<action>", ...]
}}
Return ONLY the JSON object, no prose.
"""


# ---------------------------------------------------------------------------
# InfraAgent
# ---------------------------------------------------------------------------


class InfraAgent:
    """
    Containerisation support agent.

    LLM only suggests customisation values; FileTemplateRegistry assembles
    the actual file content.
    """

    def __init__(self, provider_router: Any, file_template_registry: Any) -> None:
        self._provider = provider_router
        self._registry = file_template_registry

    # ------------------------------------------------------------------
    # Stack detection
    # ------------------------------------------------------------------

    async def detect_stack(self, workspace_path: str) -> StackType:
        """
        Heuristic stack detection by inspecting well-known files.

        Priority order (most specific first):
          requirements.txt + fastapi → PYTHON_FASTAPI
          requirements.txt + flask   → PYTHON_FLASK
          requirements.txt + django  → PYTHON_DJANGO
          package.json + next        → NODE_NEXT
          package.json + nest        → NODE_NEST
          package.json + express     → NODE_EXPRESS
          go.mod                     → GO
          pom.xml / build.gradle     → JAVA_SPRING
          Gemfile                    → RUBY_RAILS
          index.html (no server)     → STATIC
          fallback                   → UNKNOWN
        """
        ws = Path(workspace_path)

        def _read_lower(path: Path) -> str:
            try:
                return path.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                return ""

        req_txt = ws / "requirements.txt"
        pyproject = ws / "pyproject.toml"
        package_json = ws / "package.json"
        go_mod = ws / "go.mod"
        pom_xml = ws / "pom.xml"
        build_gradle = ws / "build.gradle"
        gemfile = ws / "Gemfile"

        # Python
        if req_txt.exists() or pyproject.exists():
            deps_text = _read_lower(req_txt) + _read_lower(pyproject)
            if "fastapi" in deps_text:
                return StackType.PYTHON_FASTAPI
            if "flask" in deps_text:
                return StackType.PYTHON_FLASK
            if "django" in deps_text:
                return StackType.PYTHON_DJANGO

        # Node
        if package_json.exists():
            pkg = _read_lower(package_json)
            if '"next"' in pkg or "'next'" in pkg:
                return StackType.NODE_NEXT
            if '"@nestjs' in pkg:
                return StackType.NODE_NEST
            if '"express"' in pkg:
                return StackType.NODE_EXPRESS

        if go_mod.exists():
            return StackType.GO

        if pom_xml.exists() or build_gradle.exists():
            return StackType.JAVA_SPRING

        if gemfile.exists():
            return StackType.RUBY_RAILS

        if (ws / "index.html").exists():
            return StackType.STATIC

        return StackType.UNKNOWN

    # ------------------------------------------------------------------
    # Dockerfile generation
    # ------------------------------------------------------------------

    async def generate_dockerfile(
        self,
        workspace_path: str,
        project: ProjectProfile,
    ) -> InfraFileProposal:
        """
        1. Auto-detect stack.
        2. Load base template from FileTemplateRegistry.
        3. Ask Bedrock Sonnet for customisation values.
        4. Registry assembles the final content.
        5. Return InfraFileProposal.
        """
        stack = await self.detect_stack(workspace_path)
        # Update project stack if it was unknown
        if project.stack == StackType.UNKNOWN:
            project.stack = stack

        template_id = self._pick_dockerfile_template(stack)
        try:
            template = self._registry.get(template_id)
        except Exception:
            # Fallback to python-fastapi template as a generic base
            template_id = "Dockerfile.python-fastapi"
            template = self._registry.get(template_id)

        workspace_summary = self._summarise_workspace(workspace_path)

        prompt = _DOCKERFILE_CUSTOMISE_PROMPT.format(
            stack=stack.value,
            package_manager=project.package_manager or "unknown",
            port=project.default_port or 8000,
            run_command=project.default_run_command or "auto-detect",
            health_check_path=project.health_check_path,
            template_content=template.base_content[:3000],
            workspace_summary=workspace_summary,
        )

        raw = await self._provider.complete(
            prompt=prompt,
            model_preference="sonnet",
            agent="infra_agent",
            operation="dockerfile_customise",
            max_tokens=1024,
        )

        customisations = self._extract_json_dict(raw)
        rendered_content = self._registry.render(template_id, customisations)

        required_secrets = self._detect_required_secrets(rendered_content)

        return InfraFileProposal(
            proposal_id=str(uuid.uuid4()),
            file_type=FileType.DOCKERFILE,
            target_path="Dockerfile",
            content=rendered_content,
            base_template=template_id,
            required_secrets=required_secrets,
            risk_level=RiskLevel.LOW,
            risk_reasons=[],
            approval_level=ApprovalLevel.CONFIRM,
        )

    # ------------------------------------------------------------------
    # docker-compose generation
    # ------------------------------------------------------------------

    async def generate_docker_compose(
        self,
        project: ProjectProfile,
        dockerfile_path: str,
    ) -> InfraFileProposal:
        """Generate a docker-compose.yml for the project."""
        env_var_names = self._infer_env_var_names(project)

        prompt = _COMPOSE_PROMPT.format(
            stack=project.stack.value,
            port=project.default_port or 8000,
            dockerfile_path=dockerfile_path,
            health_check_path=project.health_check_path,
            env_vars=", ".join(env_var_names) if env_var_names else "none detected",
        )

        compose_content = await self._provider.complete(
            prompt=prompt,
            model_preference="sonnet",
            agent="infra_agent",
            operation="docker_compose_generate",
            max_tokens=2048,
        )

        # Strip markdown fences if present
        compose_content = re.sub(
            r"^```[a-z]*\n?|```$", "", compose_content.strip(), flags=re.MULTILINE
        ).strip()

        required_secrets = self._detect_required_secrets(compose_content)

        return InfraFileProposal(
            proposal_id=str(uuid.uuid4()),
            file_type=FileType.DOCKER_COMPOSE,
            target_path="docker-compose.yml",
            content=compose_content,
            required_secrets=required_secrets,
            risk_level=RiskLevel.LOW,
            risk_reasons=[],
            approval_level=ApprovalLevel.CONFIRM,
        )

    # ------------------------------------------------------------------
    # Security scans
    # ------------------------------------------------------------------

    async def run_trivy_scan(self, image_name: str) -> dict[str, Any]:
        """
        One-shot Trivy container scan for critical/high CVEs.
        Returns a dict with 'raw', 'critical', 'high', and 'summary' keys.
        """
        cmd = [
            "docker", "run", "--rm",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "aquasec/trivy", "image",
            "--format", "json",
            "--severity", "CRITICAL,HIGH",
            "--quiet",
            image_name,
        ]
        returncode, stdout, stderr = await self._run_subprocess(cmd, timeout=300)

        if returncode != 0:
            logger.warning("Trivy scan failed (rc=%d): %s", returncode, stderr)
            return {
                "success": False,
                "error": stderr,
                "critical": [],
                "high": [],
                "summary": "Scan failed.",
            }

        try:
            raw_data = json.loads(stdout) if stdout.strip() else {}
        except json.JSONDecodeError:
            raw_data = {}

        critical, high = self._filter_trivy_results(raw_data)

        # Summarise with Haiku — no secret values involved
        summary = await self._summarize_scan_results(
            "trivy_image",
            {"critical_count": len(critical), "high_count": len(high),
             "critical_sample": critical[:5], "high_sample": high[:5]},
        )

        return {
            "success": True,
            "critical": critical,
            "high": high,
            "summary": summary,
            "raw": raw_data,
        }

    async def run_hadolint_scan(self, dockerfile_path: str) -> dict[str, Any]:
        """
        One-shot Hadolint container scan for Dockerfile best-practice violations.
        """
        df_path = Path(dockerfile_path)
        if not df_path.exists():
            return {"success": False, "error": f"Dockerfile not found: {dockerfile_path}"}

        content = df_path.read_bytes()

        # Pipe content via stdin
        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "--rm", "-i", "hadolint/hadolint",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=content), timeout=120
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        violations = self._parse_hadolint_output(stdout + stderr)
        summary = await self._summarize_scan_results("hadolint", {"violations": violations})

        return {
            "success": True,
            "violations": violations,
            "summary": summary,
            "raw_output": stdout,
        }

    async def run_gitleaks_scan(self, workspace_path: str) -> dict[str, Any]:
        """
        One-shot gitleaks scan for committed secrets.

        SAFETY: Secret plaintext values are NEVER passed to the LLM.
        Only file path, line number, secret type, and rule_id are forwarded.
        """
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{workspace_path}:/repo:ro",
            "zricethezav/gitleaks:latest",
            "detect",
            "--source", "/repo",
            "--report-format", "json",
            "--report-path", "/dev/stdout",
            "--no-git",
        ]
        returncode, stdout, stderr = await self._run_subprocess(cmd, timeout=180)

        # gitleaks exits 1 when findings exist; that is not an error
        if returncode not in (0, 1):
            return {"success": False, "error": stderr}

        try:
            raw_findings: list[dict] = json.loads(stdout) if stdout.strip() else []
        except json.JSONDecodeError:
            raw_findings = []

        # Sanitise: keep only metadata — strip actual secret values
        sanitised: list[dict] = []
        for finding in raw_findings:
            sanitised.append({
                "rule_id": finding.get("RuleID", "unknown"),
                "secret_type": finding.get("Description", "unknown"),
                "file": finding.get("File", "unknown"),
                "line": finding.get("StartLine", 0),
                "commit": finding.get("Commit", ""),
            })

        summary = await self._summarize_scan_results(
            "gitleaks", {"finding_count": len(sanitised), "findings": sanitised[:20]}
        )

        return {
            "success": True,
            "finding_count": len(sanitised),
            "findings": sanitised,
            "summary": summary,
        }

    async def _summarize_scan_results(
        self, scan_type: str, results: dict[str, Any]
    ) -> str:
        """Summarise scan results using Haiku (threat summary + recommended actions)."""
        findings_json = json.dumps(results, ensure_ascii=False)[:3000]
        prompt = _SCAN_SUMMARY_PROMPT.format(
            scan_type=scan_type,
            findings=findings_json,
        )
        try:
            raw = await self._provider.complete(
                prompt=prompt,
                model_preference="haiku",
                agent="infra_agent",
                operation="summarize_scan",
                max_tokens=512,
            )
            parsed = self._extract_json_dict(raw)
            return parsed.get("threat_summary", raw[:300])
        except Exception as exc:
            logger.warning("Scan summary failed: %s", exc)
            return f"Scan type: {scan_type}. Findings count: {len(str(results))}"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_dockerfile_template(stack: StackType) -> str:
        """Map StackType to the corresponding FileTemplate ID."""
        mapping = {
            StackType.PYTHON_FASTAPI: "Dockerfile.python-fastapi",
            StackType.PYTHON_FLASK: "Dockerfile.python-flask",
            StackType.PYTHON_DJANGO: "Dockerfile.python-flask",  # reuse flask as closest
            StackType.NODE_EXPRESS: "Dockerfile.node-express",
            StackType.NODE_NEXT: "Dockerfile.node-next",
            StackType.NODE_NEST: "Dockerfile.node-express",
        }
        return mapping.get(stack, "Dockerfile.python-fastapi")

    @staticmethod
    def _summarise_workspace(workspace_path: str) -> str:
        """Return a compact listing of top-level workspace files."""
        ws = Path(workspace_path)
        try:
            entries = sorted(ws.iterdir(), key=lambda p: (p.is_dir(), p.name))
            lines = [
                f"{'[dir] ' if e.is_dir() else '      '}{e.name}"
                for e in entries[:40]
            ]
            return "\n".join(lines)
        except OSError:
            return "(workspace not accessible)"

    @staticmethod
    def _detect_required_secrets(content: str) -> list[str]:
        """Detect environment variable placeholders like ${MY_SECRET} or {{MY_SECRET}}."""
        patterns = [
            re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}"),
            re.compile(r"\{\{([A-Z_][A-Z0-9_]*)\}\}"),
            re.compile(r"\$([A-Z_][A-Z0-9_]{3,})"),  # bare $VAR (≥4 chars, all caps)
        ]
        found: set[str] = set()
        for pat in patterns:
            for m in pat.finditer(content):
                found.add(m.group(1))
        return sorted(found)

    @staticmethod
    def _infer_env_var_names(project: ProjectProfile) -> list[str]:
        """Return likely env-var names based on the project stack."""
        common = ["PORT", "ENV", "LOG_LEVEL"]
        stack_extras: dict[StackType, list[str]] = {
            StackType.PYTHON_FASTAPI: ["DATABASE_URL", "SECRET_KEY", "ALLOWED_ORIGINS"],
            StackType.PYTHON_FLASK: ["FLASK_SECRET_KEY", "DATABASE_URL"],
            StackType.PYTHON_DJANGO: ["DJANGO_SECRET_KEY", "DATABASE_URL", "ALLOWED_HOSTS"],
            StackType.NODE_EXPRESS: ["NODE_ENV", "DATABASE_URL", "JWT_SECRET"],
            StackType.NODE_NEXT: ["NEXT_PUBLIC_API_URL", "DATABASE_URL"],
        }
        return common + stack_extras.get(project.stack, [])

    @staticmethod
    def _filter_trivy_results(
        raw: dict[str, Any],
    ) -> tuple[list[dict], list[dict]]:
        """Extract CRITICAL and HIGH vulnerabilities from a Trivy JSON report."""
        critical: list[dict] = []
        high: list[dict] = []

        for result in raw.get("Results", []):
            for vuln in result.get("Vulnerabilities", []):
                severity = vuln.get("Severity", "").upper()
                entry = {
                    "id": vuln.get("VulnerabilityID"),
                    "package": vuln.get("PkgName"),
                    "installed": vuln.get("InstalledVersion"),
                    "fixed": vuln.get("FixedVersion"),
                    "title": vuln.get("Title", ""),
                }
                if severity == "CRITICAL":
                    critical.append(entry)
                elif severity == "HIGH":
                    high.append(entry)

        return critical, high

    @staticmethod
    def _parse_hadolint_output(output: str) -> list[dict[str, Any]]:
        """Parse hadolint text output into a list of violation dicts."""
        violations: list[dict[str, Any]] = []
        # hadolint format: "Dockerfile:12 DL3008 warning: ..."
        pattern = re.compile(
            r"(?P<file>[^:]+):(?P<line>\d+)\s+(?P<code>DL\d+|SC\d+)\s+(?P<level>\w+):\s*(?P<msg>.+)"
        )
        for line in output.splitlines():
            m = pattern.match(line.strip())
            if m:
                violations.append({
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "code": m.group("code"),
                    "level": m.group("level"),
                    "message": m.group("msg"),
                })
        return violations

    @staticmethod
    def _extract_json_dict(text: str) -> dict[str, Any]:
        """Extract the first JSON object from an LLM response."""
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            try:
                return json.loads(fence.group(1))
            except json.JSONDecodeError:
                pass

        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        return {}

    @staticmethod
    async def _run_subprocess(
        cmd: list[str], timeout: int = 300
    ) -> tuple[int, str, str]:
        """Run a subprocess asynchronously and return (returncode, stdout, stderr)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return (
                proc.returncode or 0,
                stdout_bytes.decode("utf-8", errors="replace"),
                stderr_bytes.decode("utf-8", errors="replace"),
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return -1, "", f"Subprocess timed out after {timeout}s"
        except Exception as exc:
            return -1, "", str(exc)
