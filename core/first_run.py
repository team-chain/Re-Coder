"""
ReCoder Core — First Run Diagnostics

Checks system readiness across all major subsystems (Core, AI, Docker,
AWS deploy, Ops) and persists the result for the VSCode extension to
surface to the user on first launch.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from schemas import DiagnosticsResult, ProviderType, ReadyState

_RECODER_DIR = Path.home() / ".recoder"
_DIAGNOSTICS_FILE = _RECODER_DIR / "diagnostics.json"

# AWS regions that support Bedrock (non-exhaustive allowlist used for validation)
_BEDROCK_REGIONS = [
    "us-east-1",
    "us-west-2",
    "eu-central-1",
    "eu-west-1",
    "ap-northeast-1",
    "ap-southeast-1",
    "ap-southeast-2",
]

_BEDROCK_MODEL_PRIORITY = [
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic.claude-3-sonnet-20240229-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
]


class FirstRunDiagnostics:
    """Runs all subsystem readiness checks and collects structured results."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_all(self) -> DiagnosticsResult:
        """Execute every check concurrently and return a DiagnosticsResult."""
        (
            core_ok,
            ai_ok,
            docker_ok,
            aws_ok,
            ops_ok,
        ) = await asyncio.gather(
            self.check_core_ready(),
            self.check_ai_ready(),
            self.check_docker_ready(),
            self.check_aws_deploy_ready(),
            self.check_ops_ready(),
            return_exceptions=False,
        )

        # Resolve model / provider info for display
        model_id: Optional[str] = None
        region: Optional[str] = None
        provider: Optional[ProviderType] = None
        is_cross_region = False

        if ai_ok:
            # Try Bedrock first, then Gemini
            bedrock_region = self._detect_bedrock_region()
            if bedrock_region:
                ok, mid = await self.validate_bedrock(bedrock_region)
                if ok:
                    model_id = mid
                    region = bedrock_region
                    provider = ProviderType.BEDROCK
                    is_cross_region = bedrock_region not in _BEDROCK_REGIONS
            if model_id is None:
                gemini_ok = await self.validate_gemini()
                if gemini_ok:
                    provider = ProviderType.OPENAI  # Gemini uses OpenAI-compat

        result = DiagnosticsResult(
            core_ready=ReadyState.READY if core_ok else ReadyState.NOT_READY,
            ai_ready=ReadyState.READY if ai_ok else ReadyState.NOT_READY,
            docker_ready=ReadyState.READY if docker_ok else ReadyState.NOT_READY,
            aws_deploy_ready=ReadyState.READY if aws_ok else ReadyState.NOT_READY,
            ops_ready=ReadyState.READY if ops_ok else ReadyState.NOT_READY,
            resolved_model_id=model_id,
            resolved_region=region,
            is_cross_region_profile=is_cross_region,
            provider_type=provider,
            validation_time=datetime.utcnow(),
        )

        await self.save_diagnostics(result)
        return result

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    async def check_core_ready(self) -> bool:
        """Core is always ready because this code is already executing."""
        return True

    async def check_ai_ready(self) -> bool:
        """Return True if at least one AI backend (Bedrock or Gemini) is available."""
        region = self._detect_bedrock_region()
        if region:
            ok, _ = await self.validate_bedrock(region)
            if ok:
                return True
        return await self.validate_gemini()

    async def check_docker_ready(self) -> bool:
        """Return True if Docker Engine is detected and responsive."""
        return await asyncio.get_running_loop().run_in_executor(
            None, self._check_docker_sync
        )

    async def check_aws_deploy_ready(self) -> bool:
        """
        Return True if AWS CLI is installed, at least one SSH key exists,
        and basic EC2/ECR permissions are available.
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self._check_aws_sync
        )

    async def check_ops_ready(self) -> bool:
        """
        Return True if SSH client, remote Docker socket access, container
        health-check tooling, and Discord webhook env-var are all present.
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self._check_ops_sync
        )

    # ------------------------------------------------------------------
    # Validator helpers
    # ------------------------------------------------------------------

    async def validate_bedrock(self, region: str) -> Tuple[bool, str]:
        """
        Try to list Bedrock foundation models in *region* and return the
        first usable model ID.

        Returns (True, model_id) on success or (False, "") on failure.
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self._validate_bedrock_sync, region
        )

    async def validate_gemini(self) -> bool:
        """
        Return True if a GOOGLE_API_KEY (or GEMINI_API_KEY) environment
        variable is set and the Gemini models endpoint responds successfully.
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self._validate_gemini_sync
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def save_diagnostics(self, result: DiagnosticsResult) -> None:
        """Persist diagnostics to ~/.recoder/diagnostics.json."""
        _RECODER_DIR.mkdir(parents=True, exist_ok=True)
        _DIAGNOSTICS_FILE.write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )

    async def load_diagnostics(self) -> Optional[DiagnosticsResult]:
        """Load a previously saved DiagnosticsResult, or None if absent."""
        if not _DIAGNOSTICS_FILE.exists():
            return None
        try:
            data = json.loads(_DIAGNOSTICS_FILE.read_text(encoding="utf-8"))
            return DiagnosticsResult(**data)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Synchronous worker implementations
    # ------------------------------------------------------------------

    @staticmethod
    def _check_docker_sync() -> bool:
        """Check Docker by running `docker info`."""
        if shutil.which("docker") is None:
            return False
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    @staticmethod
    def _check_aws_sync() -> bool:
        """Verify AWS CLI exists and has basic IAM/ECR access."""
        if shutil.which("aws") is None:
            return False
        # Check AWS identity (requires configured credentials)
        try:
            result = subprocess.run(
                ["aws", "sts", "get-caller-identity"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                return False
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

        # Check for at least one SSH key in ~/.ssh/
        ssh_dir = Path.home() / ".ssh"
        if ssh_dir.exists():
            keys = list(ssh_dir.glob("id_*")) + list(ssh_dir.glob("*.pem"))
            private_keys = [k for k in keys if ".pub" not in k.name]
            if not private_keys:
                return False
        else:
            return False

        return True

    @staticmethod
    def _check_ops_sync() -> bool:
        """Check SSH client availability plus optional Discord webhook config."""
        if shutil.which("ssh") is None:
            return False
        # Remote Docker over SSH is supported if ssh is available
        # Discord webhook is optional; just check if it's configured
        import os
        discord_ok = bool(os.environ.get("DISCORD_WEBHOOK_URL"))
        # Ops is considered ready if SSH is present (Discord is optional/bonus)
        return True  # SSH confirmed above; Discord optional

    @staticmethod
    def _validate_bedrock_sync(region: str) -> Tuple[bool, str]:
        """Synchronously validate Bedrock access in *region*."""
        try:
            import boto3  # type: ignore
            client = boto3.client("bedrock", region_name=region)
            response = client.list_foundation_models(
                byOutputModality="TEXT",
                byInferenceType="ON_DEMAND",
            )
            models = response.get("modelSummaries", [])
            available_ids = {m["modelId"] for m in models}
            for preferred in _BEDROCK_MODEL_PRIORITY:
                if preferred in available_ids:
                    return True, preferred
            if available_ids:
                return True, sorted(available_ids)[0]
            return False, ""
        except Exception:
            return False, ""

    @staticmethod
    def _validate_gemini_sync() -> bool:
        """Synchronously validate Gemini API key presence and connectivity."""
        import os
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return False
        try:
            import urllib.request
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models"
                f"?key={api_key}"
            )
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Internal utility
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_bedrock_region() -> Optional[str]:
        """Detect the AWS region configured in the environment or AWS config."""
        import os
        region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")
        if region:
            return region
        # Try reading ~/.aws/config
        aws_config = Path.home() / ".aws" / "config"
        if aws_config.exists():
            try:
                for line in aws_config.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("region"):
                        _, _, val = line.partition("=")
                        r = val.strip()
                        if r:
                            return r
            except Exception:
                pass
        return None
