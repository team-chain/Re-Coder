"""
ReCoder Core — Risk Validator

Evaluates and assigns risk levels / approval requirements to:
  - PatchProposal   (code patches)
  - DeploymentPlan  (deployment actions)
  - ResponseProposal (ops remediation)

Also checks whether a deployment can be safely rolled back.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema imports
# ---------------------------------------------------------------------------


def _schemas():
    try:
        from schemas import (
            PatchProposal, DeploymentPlan, ResponseProposal, DeploymentRecord,
            RiskLevel, ApprovalLevel, ActionType, DeployMethod,
        )
    except ImportError:
        from core.schemas import (
            PatchProposal, DeploymentPlan, ResponseProposal, DeploymentRecord,
            RiskLevel, ApprovalLevel, ActionType, DeployMethod,
        )
    return (
        PatchProposal, DeploymentPlan, ResponseProposal, DeploymentRecord,
        RiskLevel, ApprovalLevel, ActionType, DeployMethod,
    )


# ---------------------------------------------------------------------------
# RiskValidator
# ---------------------------------------------------------------------------


class RiskValidator:
    """
    Stateless risk evaluation engine.

    Each public method accepts a proposal/plan model, augments it with
    re-evaluated risk_level, approval_level, and risk_reasons, and
    returns the (potentially mutated) object.
    """

    # ------------------------------------------------------------------
    # PatchProposal validation
    # ------------------------------------------------------------------

    def validate_patch_proposal(self, proposal: Any) -> Any:
        """
        Re-evaluate risk and approval level for a PatchProposal.

        Heuristics
        ----------
        - Patches touching migration / schema files -> at least HIGH
        - Patches deleting files (diff contains '--- a/' with no '+++ b/' equivalent)
          -> CRITICAL
        - More than 5 files changed -> bump risk by one level
        - Test-only changes -> LOW
        """
        (
            PatchProposal, DeploymentPlan, ResponseProposal, DeploymentRecord,
            RiskLevel, ApprovalLevel, ActionType, DeployMethod,
        ) = _schemas()

        reasons: list[str] = list(proposal.risk_reasons)
        current_risk: RiskLevel = proposal.risk_level

        for patch in proposal.patches:
            diff = patch.unified_diff.lower()
            file_lower = patch.file.lower()

            # Migration / schema risk
            if re.search(r"migration|schema|alembic|flyway|liquibase", file_lower):
                reasons.append(f"Database migration file: {patch.file}")
                current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)

            # File deletion
            if re.search(r"^--- a/", patch.unified_diff, re.MULTILINE) and not re.search(
                r"^\+\+\+ b/", patch.unified_diff, re.MULTILINE
            ):
                reasons.append(f"File deletion detected: {patch.file}")
                current_risk = _escalate(current_risk, RiskLevel.CRITICAL, RiskLevel)

            # Requirements / lock file changes
            if re.search(r"requirements.*\.txt|package-lock\.json|poetry\.lock|Pipfile\.lock", file_lower):
                reasons.append(f"Dependency file modified: {patch.file}")
                current_risk = _escalate(current_risk, RiskLevel.MEDIUM, RiskLevel)

            # Security-sensitive files
            if re.search(r"\.env|secret|credential|auth|iam", file_lower):
                reasons.append(f"Security-sensitive file: {patch.file}")
                current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)

        # Many files changed
        if len(proposal.patches) > 5:
            reasons.append(f"Large patch set: {len(proposal.patches)} files modified")
            current_risk = _bump(current_risk, RiskLevel)

        # Test-only
        all_test = all(
            re.search(r"test_|_test\.|spec\.|\.spec\.", p.file.lower())
            for p in proposal.patches
        ) and bool(proposal.patches)
        if all_test:
            current_risk = RiskLevel.LOW
            reasons.append("Test-only changes")

        approval = self._determine_approval_level(current_risk, "patch", ApprovalLevel, RiskLevel)

        proposal.risk_level = current_risk
        proposal.approval_level = approval
        proposal.risk_reasons = reasons
        return proposal

    # ------------------------------------------------------------------
    # DeploymentPlan validation
    # ------------------------------------------------------------------

    def validate_deployment_plan(self, plan: Any) -> Any:
        """
        Re-evaluate risk for a DeploymentPlan.

        Rules
        -----
        - EC2 / SSH deployment          -> Level 3 (DOUBLE_CONFIRM)
        - env variable changes          -> Level 4 (BLOCKED)
        - ECR push                      -> Level 4 (BLOCKED)
        - ECS / K8s deployment          -> Level 3
        - rollback_image absent         -> bump risk
        """
        (
            PatchProposal, DeploymentPlan, ResponseProposal, DeploymentRecord,
            RiskLevel, ApprovalLevel, ActionType, DeployMethod,
        ) = _schemas()

        reasons: list[str] = list(plan.risk_reasons)
        current_risk: RiskLevel = plan.risk_level

        # EC2 / SSH deployments are inherently higher risk
        if plan.method in (DeployMethod.SSH_DOCKER,):
            reasons.append("SSH remote deployment — direct server access")
            current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
            plan.approval_level = ApprovalLevel.DOUBLE_CONFIRM

        # ECR push — registry write
        if plan.action == ActionType.ECR_PUSH:
            reasons.append("ECR image push — immutable registry write")
            current_risk = _escalate(current_risk, RiskLevel.CRITICAL, RiskLevel)
            plan.approval_level = ApprovalLevel.BLOCKED

        # env variable modification
        if plan.env and plan.action in (
            ActionType.SSH_ENV_UPDATE, ActionType.DOCKER_RUN
        ):
            reasons.append("Environment variable modification")
            current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
            plan.approval_level = ApprovalLevel.BLOCKED

        # ECS / K8s
        if plan.method in (DeployMethod.AWS_ECS, DeployMethod.K8S):
            reasons.append("Cloud orchestration deployment")
            current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
            if plan.approval_level.value < ApprovalLevel.DOUBLE_CONFIRM.value:
                plan.approval_level = ApprovalLevel.DOUBLE_CONFIRM

        # No rollback image
        if not plan.rollback_image:
            reasons.append("No rollback image specified — cannot auto-revert")
            current_risk = _bump(current_risk, RiskLevel)

        plan.risk_level = current_risk
        plan.risk_reasons = reasons
        return plan

    # ------------------------------------------------------------------
    # ResponseProposal validation
    # ------------------------------------------------------------------

    def validate_response_proposal(
        self,
        proposal: Any,
        deployment_record: Optional[Any] = None,
    ) -> Any:
        """
        Validate an ops ResponseProposal.

        If a rollback is proposed but feasibility cannot be confirmed
        (no deployment_record or missing rollback_target), set risk to HIGH.
        """
        (
            PatchProposal, DeploymentPlan, ResponseProposal, DeploymentRecord,
            RiskLevel, ApprovalLevel, ActionType, DeployMethod,
        ) = _schemas()

        reasons: list[str] = list(proposal.risk_reasons)
        current_risk: RiskLevel = proposal.risk_level

        is_rollback_action = proposal.action_type in (
            ActionType.SSH_DOCKER_ROLLBACK,
            ActionType.DOCKER_RESTART,
        )

        if is_rollback_action:
            feasible, warnings = self.check_rollback_feasibility(
                deployment_record.deployment_id if deployment_record else ""
            )
            if not feasible:
                reasons.extend(warnings)
                current_risk = RiskLevel.HIGH
                reasons.append("Rollback feasibility not confirmed — manual review required")

        # No deployment context for a destructive action
        if deployment_record is None and is_rollback_action:
            reasons.append("No deployment record found — rollback target unknown")
            current_risk = RiskLevel.HIGH

        approval = self._determine_approval_level(
            current_risk, str(proposal.action_type.value), ApprovalLevel, RiskLevel
        )

        proposal.risk_level = current_risk
        proposal.approval_level = approval
        proposal.risk_reasons = reasons
        return proposal

    # ------------------------------------------------------------------
    # Rollback feasibility
    # ------------------------------------------------------------------

    def check_rollback_feasibility(
        self,
        deployment_id: str,
    ) -> tuple[bool, list[str]]:
        """
        Return (is_feasible, warning_messages).

        Checks
        ------
        - deployment_id is non-empty
        - Looks for a persisted DeploymentRecord in ~/.recoder/deployments/
        - Checks for DB migration flag or external resource deletion in record
        """
        import json
        from pathlib import Path

        warnings: list[str] = []

        if not deployment_id:
            return False, ["No deployment ID provided"]

        record_path = (
            Path.home() / ".recoder" / "deployments" / f"{deployment_id}.json"
        )
        if not record_path.exists():
            return False, [f"Deployment record not found: {deployment_id}"]

        try:
            data = json.loads(record_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return False, [f"Cannot read deployment record: {exc}"]

        # Check for DB migration flag
        if data.get("db_migration_applied"):
            warnings.append(
                "Database migration was applied — rollback may cause schema mismatch"
            )

        # Check for external resource deletion
        if data.get("external_resources_deleted"):
            warnings.append(
                "External resources were deleted during deployment — rollback is destructive"
            )

        # No rollback target image
        if not data.get("rollback_target"):
            warnings.append("No rollback image tag recorded")
            return False, warnings

        return len(warnings) == 0, warnings

    # ------------------------------------------------------------------
    # Approval level determination
    # ------------------------------------------------------------------

    def _determine_approval_level(
        self,
        risk_level: Any,
        action_type: str,
        ApprovalLevel: Any,
        RiskLevel: Any,
    ) -> Any:
        """
        Map risk level + action type to an ApprovalLevel.

        Level 1 (AUTO)           : Local file creation / modification, LOW risk
        Level 2 (CONFIRM)        : Local command execution, MEDIUM risk
        Level 3 (DOUBLE_CONFIRM) : Remote infra changes, HIGH risk
        Level 4 (BLOCKED)        : Sensitive config changes (env, secret, IAM, ECR push)
        """
        # Sensitive action type overrides
        sensitive_patterns = re.compile(
            r"env_update|ecr_push|iam|secret|ssh_env", re.IGNORECASE
        )
        if sensitive_patterns.search(action_type):
            return ApprovalLevel.BLOCKED

        remote_patterns = re.compile(
            r"ssh_docker|ecs|k8s|scale_up|scale_down", re.IGNORECASE
        )
        if remote_patterns.search(action_type) or risk_level == RiskLevel.HIGH:
            return ApprovalLevel.DOUBLE_CONFIRM

        risk_map = {
            RiskLevel.LOW:      ApprovalLevel.AUTO,
            RiskLevel.MEDIUM:   ApprovalLevel.CONFIRM,
            RiskLevel.HIGH:     ApprovalLevel.DOUBLE_CONFIRM,
            RiskLevel.CRITICAL: ApprovalLevel.BLOCKED,
        }
        return risk_map.get(risk_level, ApprovalLevel.CONFIRM)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_RISK_ORDER = ["low", "medium", "high", "critical"]


def _escalate(current: Any, minimum: Any, RiskLevel: Any) -> Any:
    """Return the higher of *current* and *minimum*."""
    curr_idx = _RISK_ORDER.index(current.value)
    min_idx  = _RISK_ORDER.index(minimum.value)
    return RiskLevel(_RISK_ORDER[max(curr_idx, min_idx)])


def _bump(current: Any, RiskLevel: Any) -> Any:
    """Raise *current* by one level (capped at CRITICAL)."""
    curr_idx = _RISK_ORDER.index(current.value)
    next_idx = min(curr_idx + 1, len(_RISK_ORDER) - 1)
    return RiskLevel(_RISK_ORDER[next_idx])
