"""
ReCoder Q1 — Executor (결정론적 디스패처)

설계서 v5.0 §Plan-Execute-Verify:

Executor는 LLM이 아니다.
action 타입에 따라 CodeAgent / InfraAgent / DeployAgent / TestRunner를 호출한다.
컨텍스트를 임의로 확장하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from schemas import (
    AgentType,
    AnalyzeRequest,
    ExecutionPlan,
    ExecutionStep,
    PatchProposal,
    RiskLevel,
)

logger = logging.getLogger(__name__)

# test_command 실행 타임아웃 (설계서: 30초 필수)
_TEST_TIMEOUT_SECONDS = 30

# Shell metacharacter 차단 패턴
_SHELL_META = set(';|&`$><(){}[]\\!')


# ---------------------------------------------------------------------------
# StepResult
# ---------------------------------------------------------------------------


class StepResult:
    """Result of a single Executor step."""

    def __init__(
        self,
        step_id: str,
        agent: AgentType,
        success: bool,
        output: Any = None,
        error: Optional[str] = None,
        duration_seconds: float = 0.0,
    ) -> None:
        self.step_id = step_id
        self.agent = agent
        self.success = success
        self.output = output
        self.error = error
        self.duration_seconds = duration_seconds

    def __repr__(self) -> str:
        status = "OK" if self.success else "FAIL"
        return f"StepResult({self.step_id} {self.agent.value} {status})"


class ExecutionResult:
    """Aggregated result of running an entire ExecutionPlan."""

    def __init__(self, plan_id: str) -> None:
        self.plan_id = plan_id
        self.steps: list[StepResult] = []
        self.patch_proposal: Optional[PatchProposal] = None
        self.aborted = False
        self.abort_reason: Optional[str] = None

    @property
    def success(self) -> bool:
        return not self.aborted and all(s.success for s in self.steps)

    @property
    def last_step(self) -> Optional[StepResult]:
        return self.steps[-1] if self.steps else None


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class Executor:
    """
    Deterministic dispatcher for ExecutionPlan steps.

    - Never calls an LLM directly.
    - Routes each step to the appropriate agent based on AgentType.
    - Respects step dependencies (depends_on).
    - Stops on first failure and marks ExecutionResult.aborted = True.
    """

    def __init__(self, provider_router: Any, context_gate: Any) -> None:
        self._router = provider_router
        self._gate = context_gate

    async def execute(
        self,
        plan: ExecutionPlan,
        request: AnalyzeRequest,
    ) -> ExecutionResult:
        """
        Run all steps in dependency order.
        Aborts on first step failure.
        """
        result = ExecutionResult(plan_id=plan.plan_id)

        # Build step lookup for dependency resolution
        completed_ids: set[str] = set()
        step_map: dict[str, ExecutionStep] = {s.step_id: s for s in plan.steps}

        for step in plan.steps:
            # Wait for dependencies (steps already ran — just check completion)
            for dep_id in step.depends_on:
                if dep_id not in completed_ids:
                    result.aborted = True
                    result.abort_reason = (
                        f"Step {step.step_id} depends on {dep_id} which did not complete"
                    )
                    logger.error(result.abort_reason)
                    return result

            step_result = await self._run_step(step, request, result)
            result.steps.append(step_result)

            if not step_result.success:
                result.aborted = True
                result.abort_reason = f"Step {step.step_id} failed: {step_result.error}"
                logger.error("Aborting plan %s: %s", plan.plan_id, result.abort_reason)
                return result

            completed_ids.add(step.step_id)

            # Capture PatchProposal from CodeAgent output
            if (
                step.agent == AgentType.CODE_AGENT
                and isinstance(step_result.output, PatchProposal)
            ):
                result.patch_proposal = step_result.output

        return result

    # ------------------------------------------------------------------
    # Step dispatch
    # ------------------------------------------------------------------

    async def _run_step(
        self,
        step: ExecutionStep,
        request: AnalyzeRequest,
        result: ExecutionResult,
    ) -> StepResult:
        t0 = time.monotonic()
        logger.info("Executing step %s (%s): %s", step.step_id, step.agent.value, step.description)

        try:
            if step.agent == AgentType.CODE_AGENT:
                output = await self._run_code_agent(step, request)
            elif step.agent == AgentType.INFRA_AGENT:
                output = await self._run_infra_agent(step, request)
            elif step.agent == AgentType.DEPLOY_AGENT:
                output = await self._run_deploy_agent(step, request)
            elif step.agent == AgentType.TEST_RUNNER:
                output = await self._run_test_runner(step, request)
            elif step.agent == AgentType.NO_OP:
                output = {"message": "no_op step — manual action required"}
            else:
                raise ValueError(f"Unknown agent type: {step.agent}")

            return StepResult(
                step_id=step.step_id,
                agent=step.agent,
                success=True,
                output=output,
                duration_seconds=time.monotonic() - t0,
            )

        except Exception as exc:
            logger.exception("Step %s failed: %s", step.step_id, exc)
            return StepResult(
                step_id=step.step_id,
                agent=step.agent,
                success=False,
                error=str(exc),
                duration_seconds=time.monotonic() - t0,
            )

    # ------------------------------------------------------------------
    # Agent runners
    # ------------------------------------------------------------------

    async def _run_code_agent(
        self, step: ExecutionStep, request: AnalyzeRequest
    ) -> PatchProposal:
        from agents.code_agent import CodeAgent  # type: ignore

        # Merge step args into request (step args take precedence)
        merged = AnalyzeRequest(
            workspace_path=step.args.get("workspace_path", request.workspace_path),
            active_file_path=step.args.get("active_file_path", request.active_file_path),
            terminal_output=step.args.get("terminal_output", request.terminal_output),
            command=step.args.get("command", request.command),
            project_files_summary=step.args.get(
                "project_files_summary", request.project_files_summary
            ),
            project_id=request.project_id,
        )

        masking = await self._gate.mask(
            "\n".join(filter(None, [merged.terminal_output, merged.selected_text]))
        )

        # Minimal ProjectProfile for CodeAgent
        from schemas import ProjectProfile, StackType
        profile = ProjectProfile(
            workspace_path=merged.workspace_path,
            stack=StackType.UNKNOWN,
        )

        agent = CodeAgent(self._router)
        return await agent.analyze_and_propose(masking.masked_content, merged, profile)

    async def _run_infra_agent(
        self, step: ExecutionStep, request: AnalyzeRequest
    ) -> Any:
        from agents.infra_agent import InfraAgent  # type: ignore

        workspace = step.args.get("workspace_path", request.workspace_path)
        project_id = step.args.get("project_id", request.project_id or "")
        agent = InfraAgent(self._router)
        return await agent.generate(workspace, project_id)

    async def _run_deploy_agent(
        self, step: ExecutionStep, request: AnalyzeRequest
    ) -> Any:
        from agents.deploy_agent import DeployAgent  # type: ignore
        from schemas import DeploymentPlan

        plan_data = step.args.get("plan")
        if not plan_data:
            raise ValueError("deploy_agent step requires 'plan' in args")
        deploy_plan = DeploymentPlan(**plan_data)
        agent = DeployAgent(self._router)
        return await agent.execute(deploy_plan)

    async def _run_test_runner(
        self, step: ExecutionStep, request: AnalyzeRequest
    ) -> dict[str, Any]:
        """
        Run test_command in a subprocess with 30-second timeout.

        Constraints (설계서):
        - package.json scripts와 pyproject.toml 기반 명령만 허용
        - shell metacharacter 차단
        - 타임아웃 30초 필수
        - working directory = 프로젝트 루트
        """
        cmd = step.args.get("command", "")
        if not cmd:
            return {"skipped": True, "reason": "No command provided"}

        # Shell metacharacter check
        if any(ch in cmd for ch in _SHELL_META):
            raise ValueError(f"Shell metacharacter detected in test command: {cmd!r}")

        workspace = step.args.get("workspace_path", request.workspace_path)
        cwd = Path(workspace) if workspace else Path.cwd()

        # Allowlist: only npm/yarn/pnpm scripts or python/pytest commands
        allowed_prefixes = (
            "npm ", "yarn ", "pnpm ", "npx ",
            "python ", "python3 ", "pytest", "py.test",
            "poetry run ", "uv run ",
        )
        if not any(cmd.startswith(p) for p in allowed_prefixes):
            raise ValueError(
                f"test_command '{cmd}' not in allowlist. "
                f"Use npm/yarn/python/pytest-based commands only."
            )

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd.split(),
                    cwd=str(cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                ),
                timeout=_TEST_TIMEOUT_SECONDS,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_TEST_TIMEOUT_SECONDS
            )
            returncode = proc.returncode or 0
            output = stdout.decode("utf-8", errors="replace")[:4096]
            return {
                "command": cmd,
                "returncode": returncode,
                "output": output,
                "passed": returncode == 0,
            }
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"test_command '{cmd}' exceeded {_TEST_TIMEOUT_SECONDS}s timeout"
            )
