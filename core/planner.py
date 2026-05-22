"""
ReCoder Q1 — PlannerAgent

설계서 v5.0 §Plan-Execute-Verify:

PlannerAgent (Bedrock Sonnet):
- 최대 5단계 ExecutionPlan을 Structured Output으로 생성
- 절대 실행하지 않는다 — 계획만 수립
- Executor(결정론적 디스패처)가 실제 에이전트를 호출함
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from schemas import (
    AgentType,
    AnalyzeRequest,
    ExecutionPlan,
    ExecutionStep,
    RiskLevel,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM = (
    "You are ReCoder PlannerAgent. Your ONLY job is to produce an ExecutionPlan. "
    "You must NEVER execute code, run commands, or produce file patches. "
    "Respond with a single JSON object conforming to the schema. No prose."
)

_PLANNER_PROMPT_TMPL = """\
## Context
Workspace: {workspace_path}
Active file: {active_file}
Terminal output / error:
```
{terminal_output}
```
Project files summary:
{project_files_summary}

## Task
Produce a minimal ExecutionPlan (maximum 5 steps) to fix the error above.

## Available agents
- code_agent   : Analyse errors and produce file patches (PatchProposal)
- infra_agent  : Generate Dockerfile / docker-compose / infra files
- deploy_agent : Execute deployment plans (Docker / ECS)
- test_runner  : Run test_command and report results
- no_op        : Placeholder for steps that require human action

## ExecutionPlan JSON Schema
{{
  "schema_version": "1.0",
  "plan_id": "<uuid4>",
  "summary": "<one-sentence plan description>",
  "estimated_risk": "low" | "medium" | "high" | "critical",
  "requires_approval": true | false,
  "steps": [
    {{
      "step_id": "<short id>",
      "description": "<what this step does>",
      "agent": "<agent name from above>",
      "args": {{}},
      "depends_on": []
    }}
  ]
}}

Rules:
1. Maximum 5 steps.
2. steps must be ordered: dependencies come before dependents.
3. requires_approval must be true when estimated_risk is high or critical.
4. Return ONLY the JSON object.
"""

# ---------------------------------------------------------------------------
# PlannerAgent
# ---------------------------------------------------------------------------


class PlannerAgent:
    """
    Converts an AnalyzeRequest into an ExecutionPlan using the LLM.

    The returned plan is handed to Executor for deterministic dispatch.
    PlannerAgent itself never touches the filesystem or runs subprocesses.
    """

    # Hard cap: never exceed 5 steps (설계서 constraint)
    _MAX_STEPS = 5

    def __init__(self, provider_router: Any) -> None:
        self._provider = provider_router

    async def plan(self, request: AnalyzeRequest) -> ExecutionPlan:
        """
        Call the LLM and return a validated ExecutionPlan.

        Falls back to a single code_agent step on LLM / parse failure.
        """
        prompt = self._build_prompt(request)

        try:
            raw = await self._provider.complete(
                prompt=prompt,
                system=_PLANNER_SYSTEM,
                model_preference="sonnet",
                agent="planner_agent",
                operation="plan",
                max_tokens=2048,
            )
            raw_dict = _extract_json(raw)
            return self._validate_plan(raw_dict)
        except Exception as exc:
            logger.warning("PlannerAgent LLM call failed (%s) — using fallback plan", exc)
            return self._fallback_plan(request)

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(request: AnalyzeRequest) -> str:
        return _PLANNER_PROMPT_TMPL.format(
            workspace_path=request.workspace_path,
            active_file=request.active_file_path or "(none)",
            terminal_output=(request.terminal_output or "")[:3000],
            project_files_summary=(request.project_files_summary or "")[:500],
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_plan(self, raw: dict[str, Any]) -> ExecutionPlan:
        raw.setdefault("schema_version", "1.0")
        raw.setdefault("plan_id", str(uuid.uuid4()))
        raw.setdefault("summary", "AI-generated execution plan")
        raw.setdefault("estimated_risk", RiskLevel.LOW.value)
        raw.setdefault("requires_approval", False)
        raw.setdefault("steps", [])

        # Coerce risk level
        try:
            risk = RiskLevel(raw["estimated_risk"])
        except ValueError:
            risk = RiskLevel.MEDIUM
        raw["estimated_risk"] = risk

        # Force approval for high/critical risk
        if risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            raw["requires_approval"] = True

        # Validate and cap steps
        valid_steps: list[ExecutionStep] = []
        for s in raw.get("steps", [])[: self._MAX_STEPS]:
            if not isinstance(s, dict):
                continue
            s.setdefault("step_id", str(uuid.uuid4())[:8])
            s.setdefault("description", "")
            s.setdefault("args", {})
            s.setdefault("depends_on", [])

            # Coerce agent
            try:
                agent = AgentType(s.get("agent", AgentType.CODE_AGENT.value))
            except ValueError:
                agent = AgentType.CODE_AGENT
            s["agent"] = agent

            valid_steps.append(ExecutionStep(**s))

        raw["steps"] = valid_steps
        return ExecutionPlan(**raw)

    @staticmethod
    def _fallback_plan(request: AnalyzeRequest) -> ExecutionPlan:
        """Single code_agent step — used when LLM call fails."""
        return ExecutionPlan(
            summary="Fallback: analyze error with CodeAgent",
            steps=[
                ExecutionStep(
                    description="Analyze terminal error and propose patch",
                    agent=AgentType.CODE_AGENT,
                    args={
                        "workspace_path": request.workspace_path,
                        "active_file_path": request.active_file_path or "",
                        "terminal_output": request.terminal_output or "",
                    },
                )
            ],
            estimated_risk=RiskLevel.LOW,
            requires_approval=False,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict[str, Any]:
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

    raise ValueError(f"No JSON found in LLM response: {text[:200]}")
