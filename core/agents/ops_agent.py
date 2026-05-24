"""
ReCoder Ops Agent — Incident analysis and ResponseProposal generation.

Design principles:
- Ops agent READS data (via SSH or Watchdog API) but NEVER executes remotely.
- LLM analyses sanitised (masked) incident data only.
- ResponseProposal is the only output; execution requires user approval.
- gitleaks / secret values are never sent to LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema imports
# ---------------------------------------------------------------------------


def _schemas():
    try:
        from schemas import (
            ActionType, AlertRecord, AlertType, ApprovalLevel,
            ResponseProposal, RiskLevel,
        )
    except ImportError:
        from core.schemas import (
            ActionType, AlertRecord, AlertType, ApprovalLevel,
            ResponseProposal, RiskLevel,
        )
    return ActionType, AlertRecord, AlertType, ApprovalLevel, ResponseProposal, RiskLevel


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_DIAGNOSE_PROMPT = """\
You are ReCoder, an AI DevOps assistant specialising in production incident response.

## Task
Analyse the operational incident below and propose a remediation action.

## Alert details
- Alert type: {alert_type}
- Severity: {severity}
- Container: {container_name}
- Environment: {environment}
- Detected at: {detected_at}

## Log excerpt (sanitised)
```
{logs_excerpt}
```

## Health check result
{health_check}

## Recent deployment context
{deployment_context}

## Output format
Return ONLY a JSON object:
{{
  "action_type": "restart" | "rollback" | "env_check" | "no_action",
  "reasoning": "<2-3 sentence explanation>",
  "risk_level": "low" | "medium" | "high" | "critical",
  "risk_reasons": ["<reason>"],
  "requires_approval_level": 2 | 3 | 4,
  "rollback_feasible": true | false,
  "rollback_blocker": "<reason if not feasible or null>"
}}
"""


class OpsAgent:
    """
    Incident analysis and response proposal generation.

    Workflow:
    1. Receive an AlertRecord (from Watchdog via SSH or direct fetch).
    2. Apply Context Gate masking to log excerpts.
    3. Invoke Bedrock Sonnet to diagnose root cause.
    4. Return a ResponseProposal for user approval.
    """

    def __init__(self, provider_router: Any = None) -> None:
        self._provider = provider_router

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def diagnose(self, alert: Any) -> Any:
        """
        Analyse an AlertRecord and generate a ResponseProposal.

        *alert* can be an AlertRecord instance or a plain dict.
        """
        ActionType, AlertRecord, AlertType, ApprovalLevel, ResponseProposal, RiskLevel = _schemas()

        # Normalise to AlertRecord
        if isinstance(alert, dict):
            alert_obj = AlertRecord(**alert)
        else:
            alert_obj = alert

        # Mask logs before sending to LLM
        logs = await self._mask_logs(alert_obj.logs_excerpt or "")

        # Build diagnosis prompt
        health_str = "N/A"
        if alert_obj.health_check_result:
            hc = alert_obj.health_check_result
            if hasattr(hc, 'model_dump'):
                hc_dict = hc.model_dump()
            elif hasattr(hc, 'dict'):
                hc_dict = hc.dict()
            else:
                hc_dict = dict(hc)
            health_str = json.dumps(hc_dict, default=str)

        prompt = _DIAGNOSE_PROMPT.format(
            alert_type=alert_obj.alert_type.value if hasattr(alert_obj.alert_type, 'value') else str(alert_obj.alert_type),
            severity=alert_obj.severity.value if hasattr(alert_obj.severity, 'value') else str(alert_obj.severity),
            container_name=alert_obj.container_name or "unknown",
            environment=alert_obj.environment,
            detected_at=str(alert_obj.detected_at),
            logs_excerpt=logs[:2000] if logs else "(no logs available)",
            health_check=health_str,
            deployment_context=f"Recent deployment ID: {alert_obj.recent_deployment_id or 'none'}",
        )

        # Call LLM
        raw_result = await self._call_llm(prompt)

        # Build ResponseProposal
        return self._build_proposal(raw_result, alert_obj, ActionType, ApprovalLevel, ResponseProposal, RiskLevel)

    async def fetch_incidents_ssh(
        self,
        host: str,
        ssh_key_path: str,
        ssh_user: str = "ec2-user",
        ssh_port: int = 22,
        incident_log_path: str = "/var/log/recoder/incidents.jsonl",
        limit: int = 50,
    ) -> list[Any]:
        """
        Fetch incident records from a remote EC2 via SSH.

        Returns a list of AlertRecord dicts (still masked on the remote side).
        """
        # SSH command: tail last N lines of the incident JSONL
        ssh_cmd = [
            "ssh",
            "-i", ssh_key_path,
            "-p", str(ssh_port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{ssh_user}@{host}",
            f"tail -n {limit} {incident_log_path} 2>/dev/null || echo '[]'",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=30)
            raw = stdout_b.decode("utf-8", errors="replace").strip()
        except asyncio.TimeoutError:
            logger.warning("SSH connection to %s timed out", host)
            return []
        except Exception as exc:
            logger.error("SSH fetch failed: %s", exc)
            return []

        records = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line == "[]":
                continue
            try:
                data = json.loads(line)
                records.append(data)
            except json.JSONDecodeError:
                continue

        return records

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _mask_logs(self, logs: str) -> str:
        """Apply Context Gate masking to log content."""
        if not logs:
            return ""
        try:
            try:
                from context_gate import ContextGate
            except ImportError:
                from core.context_gate import ContextGate
            gate = ContextGate()
            result = await gate.mask(logs)
            return result.masked_content
        except Exception as exc:
            logger.warning("Context Gate masking failed: %s", exc)
            return logs

    async def _call_llm(self, prompt: str) -> dict[str, Any]:
        """Call the LLM provider (Bedrock Sonnet) for diagnosis."""
        if self._provider is None:
            return self._fallback_response()

        try:
            raw = await self._provider.complete(
                prompt=prompt,
                model_preference="sonnet",
                agent="ops_agent",
                operation="diagnose_incident",
                max_tokens=1024,
            )
            return self._extract_json(raw)
        except Exception as exc:
            logger.error("LLM call failed in OpsAgent: %s", exc)
            return self._fallback_response()

    @staticmethod
    def _fallback_response() -> dict[str, Any]:
        return {
            "action_type": "restart",
            "reasoning": "Unable to reach AI provider. Recommending container restart as default safe action.",
            "risk_level": "medium",
            "risk_reasons": ["AI analysis unavailable — default action"],
            "requires_approval_level": 3,
            "rollback_feasible": False,
            "rollback_blocker": "No AI analysis available",
        }

    @staticmethod
    def _build_proposal(
        raw: dict[str, Any],
        alert: Any,
        ActionType: Any,
        ApprovalLevel: Any,
        ResponseProposal: Any,
        RiskLevel: Any,
    ) -> Any:
        """Convert LLM output dict into a validated ResponseProposal."""
        action_str = raw.get("action_type", "no_action")
        action_map = {
            "restart": ActionType.DOCKER_RESTART,
            "rollback": ActionType.SSH_DOCKER_ROLLBACK,
            "env_check": ActionType.SSH_ENV_UPDATE,
            "no_action": ActionType.NO_ACTION,
        }
        action = action_map.get(action_str, ActionType.NO_ACTION)

        raw_risk = raw.get("risk_level", "medium")
        try:
            risk = RiskLevel(raw_risk)
        except ValueError:
            risk = RiskLevel.MEDIUM

        approval_level_int = raw.get("requires_approval_level", 3)
        try:
            approval = ApprovalLevel(int(approval_level_int))
        except (ValueError, TypeError):
            approval = ApprovalLevel.DOUBLE_CONFIRM

        template_map = {
            ActionType.DOCKER_RESTART: "ssh_docker_restart",
            ActionType.SSH_DOCKER_ROLLBACK: "ssh_docker_rollback",
            ActionType.SSH_ENV_UPDATE: "ssh_env_update",
        }
        template_id = template_map.get(action)

        return ResponseProposal(
            alert_id=alert.alert_id,
            action_type=action,
            target_container=alert.container_name,
            command_template_id=template_id,
            parameters={
                "reasoning": raw.get("reasoning", ""),
                "rollback_feasible": raw.get("rollback_feasible", False),
                "rollback_blocker": raw.get("rollback_blocker"),
            },
            risk_level=risk,
            risk_reasons=raw.get("risk_reasons", []),
            approval_level=approval,
        )

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """Extract the first JSON object from an LLM response string."""
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
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        return {}
