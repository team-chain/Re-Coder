"""
ReCoder Q1 — Plan-Execute-Verify 파이프라인 조율자

설계서 v5.0:
  PlannerAgent → Executor → VerifierAgent
  재시도 최대 2회 (Execute → Verify 루프)
  소진 시 "자동 검증 실패, 수동 검토 필요" 상태로 AWAITING_APPROVAL 진입
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from schemas import (
    AnalyzeRequest,
    ApprovalLevel,
    ExecutionPlan,
    PatchProposal,
    RiskLevel,
    VerificationResult,
)

logger = logging.getLogger(__name__)

_MAX_RETRY = 2


class PlanExecuteVerifyPipeline:
    """
    Orchestrates the full Plan → Execute → Verify loop.

    Usage::

        pipeline = PlanExecuteVerifyPipeline(provider_router, context_gate)
        result = await pipeline.run(request)
        # result.proposal      : PatchProposal | None
        # result.verification  : VerificationResult | None
        # result.needs_approval: bool
        # result.message       : str (for Sidebar display)
    """

    def __init__(self, provider_router: Any, context_gate: Any) -> None:
        self._router = provider_router
        self._gate = context_gate

    async def run(self, request: AnalyzeRequest) -> "PipelineResult":
        from planner import PlannerAgent
        from executor import Executor
        from verifier import VerifierAgent

        planner = PlannerAgent(self._router)
        executor = Executor(self._router, self._gate)
        verifier = VerifierAgent()

        # 1. Plan
        plan: ExecutionPlan = await planner.plan(request)
        logger.info(
            "PEV pipeline: plan %s (%d steps, risk=%s)",
            plan.plan_id[:8],
            len(plan.steps),
            plan.estimated_risk.value,
        )

        proposal: Optional[PatchProposal] = None
        verification: Optional[VerificationResult] = None

        # 2. Execute → Verify loop (최대 2회)
        for attempt in range(_MAX_RETRY + 1):
            exec_result = await executor.execute(plan, request)

            if exec_result.aborted:
                logger.warning(
                    "PEV: executor aborted (attempt %d): %s",
                    attempt + 1,
                    exec_result.abort_reason,
                )
                return PipelineResult(
                    plan=plan,
                    proposal=None,
                    verification=None,
                    needs_approval=True,
                    message=f"자동 검증 실패, 수동 검토 필요: {exec_result.abort_reason}",
                    needs_manual_review=True,
                )

            proposal = exec_result.patch_proposal

            if proposal is None:
                # No patch produced — treat as success (e.g., infra-only step)
                logger.info("PEV: no PatchProposal produced — non-code plan")
                return PipelineResult(
                    plan=plan,
                    proposal=None,
                    verification=None,
                    needs_approval=plan.requires_approval,
                    message="실행 완료 (패치 없음)",
                    needs_manual_review=False,
                )

            # 3. Verify
            verification = await verifier.verify(
                proposal=proposal,
                plan_id=plan.plan_id,
                workspace_path=request.workspace_path,
                retry_count=attempt,
            )

            if verification.needs_manual_review:
                return PipelineResult(
                    plan=plan,
                    proposal=proposal,
                    verification=verification,
                    needs_approval=True,
                    message="자동 검증 실패, 수동 검토 필요",
                    needs_manual_review=True,
                )

            all_ok = (
                verification.schema_valid
                and verification.sha256_valid
                and (verification.test_passed is None or verification.test_passed)
            )

            if all_ok:
                logger.info("PEV: verification passed on attempt %d", attempt + 1)
                needs_approval = (
                    plan.requires_approval
                    or proposal.approval_level.value >= ApprovalLevel.CONFIRM.value
                )
                return PipelineResult(
                    plan=plan,
                    proposal=proposal,
                    verification=verification,
                    needs_approval=needs_approval,
                    message="검증 완료",
                    needs_manual_review=False,
                )

            logger.warning(
                "PEV: verification failed (attempt %d/%d) — retrying",
                attempt + 1,
                _MAX_RETRY + 1,
            )

        # Should not reach here
        return PipelineResult(
            plan=plan,
            proposal=proposal,
            verification=verification,
            needs_approval=True,
            message="자동 검증 실패, 수동 검토 필요",
            needs_manual_review=True,
        )


class PipelineResult:
    """Return value of PlanExecuteVerifyPipeline.run()."""

    def __init__(
        self,
        plan: ExecutionPlan,
        proposal: Optional[PatchProposal],
        verification: Optional[VerificationResult],
        needs_approval: bool,
        message: str,
        needs_manual_review: bool,
    ) -> None:
        self.plan = plan
        self.proposal = proposal
        self.verification = verification
        self.needs_approval = needs_approval
        self.message = message
        self.needs_manual_review = needs_manual_review

    @property
    def success(self) -> bool:
        return self.proposal is not None and not self.needs_manual_review
