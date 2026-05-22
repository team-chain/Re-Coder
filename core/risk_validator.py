"""
<<<<<<< HEAD
ReCoder Core — Risk Validator

Evaluates and assigns risk levels / approval requirements to:
  - PatchProposal   (code patches)
  - DeploymentPlan  (deployment actions)
  - ResponseProposal (ops remediation)

Also checks whether a deployment can be safely rolled back.
=======
Risk Validator (v6.4 §17).

Proposal의 risk_level을 검증하고 rollback 가능성을 평가한다.
- PatchProposal의 위험도 재평가 (다중 파일 수정 시 medium 이상)
- InfraFileProposal 검증 (privileged/host mount 감지 시 high 격상)
- DeploymentPlan 검증 (원격 배포 시 Level 3 강제)
- Rollback 가능 조건 체크
>>>>>>> 74cf4369799da45d0fa49de67d56e58e01a2cc27
"""

from __future__ import annotations

<<<<<<< HEAD
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

        # Always re-derive approval_level from final risk (§16 Approval Level)
        # Specific branches above may have already set a stricter level;
        # only upgrade (never downgrade) to match the resolved risk.
        derived = self._determine_approval_level(
            current_risk,
            str(plan.action.value) if plan.action else "unknown",
            ApprovalLevel,
            RiskLevel,
        )
        if derived.value > plan.approval_level.value:
            plan.approval_level = derived

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
=======
from dataclasses import dataclass, field
from typing import Optional

from schemas import (
    DeploymentPlan, DeploymentRecord, InfraFileProposal, PatchProposal,
    ResponseProposal, RiskLevel
)


@dataclass
class RollbackAssessment:
    """rollback 가능성 평가 결과"""
    can_rollback: bool
    reason: str
    warnings: list[str] = field(default_factory=list)


class RiskValidator:
    """다양한 Proposal 타입의 위험도를 검증하는 클래스."""

    def validate_patch(self, proposal: PatchProposal) -> PatchProposal:
        """
        patch의 risk_level 재평가.

        규칙:
        - 파일 1개 & 단순 수정 → low
        - 파일 2-3개 → medium
        - 파일 4개 이상 → high
        - 위험한 패턴 감지 (import 제거, 루트 디렉터리 삭제 등) → high

        인자:
            proposal: PatchProposal

        반환:
            업데이트된 PatchProposal
        """
        if not proposal.patches:
            # 패치가 없으면 low
            proposal.risk_level = RiskLevel.LOW
            proposal.risk_reasons = ["No patches to apply"]
            return proposal

        patch_count = len(proposal.patches)
        risk_reasons: list[str] = []

        # 파일 개수에 따른 기본 위험도
        if patch_count == 1:
            base_risk = RiskLevel.LOW
            risk_reasons.append(f"Single file patch: {proposal.patches[0].file}")
        elif patch_count <= 3:
            base_risk = RiskLevel.MEDIUM
            risk_reasons.append(f"Multiple files ({patch_count}): Medium risk")
        else:
            base_risk = RiskLevel.HIGH
            risk_reasons.append(f"Multiple files ({patch_count}): Escalated to HIGH")

        # 위험한 패턴 감지
        for patch in proposal.patches:
            file_path = patch.file.lower()
            diff = patch.unified_diff.lower()

            # 설정/보안 관련 파일
            if any(x in file_path for x in ['secret', 'password', 'token', 'key', '.env']):
                risk_reasons.append(f"⚠ Security-sensitive file: {patch.file}")
                base_risk = RiskLevel.HIGH

            # 시스템 파일
            if any(x in file_path for x in ['/etc/', '/sys/', '/boot/', 'dockerfile']):
                risk_reasons.append(f"⚠ System file: {patch.file}")
                base_risk = RiskLevel.HIGH

            # 대량 삭제
            if diff.count('\n-') > 50:
                risk_reasons.append(f"⚠ Large deletion in {patch.file}: {diff.count(chr(10) + '-')} lines removed")
                base_risk = RiskLevel.HIGH

            # 의존성 제거 패턴
            if any(x in diff for x in ['import ', 'require', 'include', 'from ']):
                removals = [line for line in patch.unified_diff.split('\n') if line.startswith('-')]
                if any('import' in line.lower() or 'require' in line.lower() for line in removals):
                    risk_reasons.append(f"⚠ Dependency removal detected in {patch.file}")
                    base_risk = RiskLevel.HIGH

        proposal.risk_level = base_risk
        proposal.risk_reasons = risk_reasons
        return proposal

    def validate_infra(self, proposal: InfraFileProposal) -> InfraFileProposal:
        """
        Dockerfile/Kubernetes 매니페스트 검증.

        위험 패턴:
        - privileged 컨테이너
        - host 네트워크/PID/IPC
        - host mount (/etc, /sys 등)
        - root 사용자
        - 버전 없는 이미지

        인자:
            proposal: InfraFileProposal

        반환:
            업데이트된 InfraFileProposal (risk_level 갱신)
        """
        content = proposal.content or ""
        risk_reasons: list[str] = []
        detected_issues = 0

        # Dockerfile 검사
        if proposal.file_type == "dockerfile":
            content_lower = content.lower()

            # privileged 플래그
            if 'privileged=true' in content_lower or '--privileged' in content_lower:
                risk_reasons.append("⚠ Privileged container detected")
                detected_issues += 1

            # host 네트워크
            if 'network=host' in content_lower or 'net=host' in content_lower:
                risk_reasons.append("⚠ Host network mode detected")
                detected_issues += 1

            # 루트 사용
            if 'user root' in content_lower or not ('user ' in content_lower):
                risk_reasons.append("⚠ Container runs as root")
                detected_issues += 1

            # 버전 없는 이미지
            if 'from ' in content_lower:
                import re
                from_lines = [line for line in content.split('\n') if line.lower().startswith('from ')]
                for line in from_lines:
                    if ':' not in line and 'as' not in line.lower():
                        risk_reasons.append(f"⚠ Unversioned base image: {line.strip()}")
                        detected_issues += 1

            # host mount
            if any(x in content_lower for x in ['mount_path /etc', 'mount_path /sys', 'mount_path /proc']):
                risk_reasons.append("⚠ System directory mount detected")
                detected_issues += 1

        # Kubernetes 매니페스트 검사
        elif proposal.file_type == "kubernetes":
            content_lower = content.lower()

            # privileged securityContext
            if 'privileged: true' in content_lower:
                risk_reasons.append("⚠ Privileged pod detected")
                detected_issues += 1

            # host 접근
            if 'hostpid: true' in content_lower or 'hostipc: true' in content_lower:
                risk_reasons.append("⚠ Host PID/IPC access detected")
                detected_issues += 1

            if 'hostnetwork: true' in content_lower:
                risk_reasons.append("⚠ Host network access detected")
                detected_issues += 1

            # host path volume
            if 'hostpath:' in content_lower:
                risk_reasons.append("⚠ HostPath volume detected")
                detected_issues += 1

        # 위험도 결정
        if detected_issues >= 3:
            proposal.risk_level = RiskLevel.HIGH
        elif detected_issues >= 1:
            proposal.risk_level = RiskLevel.MEDIUM
        else:
            proposal.risk_level = RiskLevel.LOW

        proposal.risk_reasons = risk_reasons
        return proposal

    def validate_deploy(
        self,
        plan: DeploymentPlan,
        last_record: DeploymentRecord | None = None,
    ) -> DeploymentPlan:
        """
        배포 계획 검증.

        규칙:
        - 로컬 배포 → Level 1-2 가능
        - 원격 배포 → Level 3 강제
        - rollback_image 없으면 위험도 상향 및 경고

        인자:
            plan: DeploymentPlan
            last_record: 마지막 배포 기록 (선택)

        반환:
            업데이트된 DeploymentPlan
        """
        risk_reasons = plan.risk_reasons or []

        # 원격 배포 감지
        is_remote = self._is_remote_deployment(plan)
        if is_remote:
            # 원격 배포는 최소 Level 3
            if plan.approval_level < 3:
                plan.approval_level = 3
                risk_reasons.append("Remote deployment detected → approval_level=3")

        # rollback_image 검증
        if not plan.rollback_image:
            risk_reasons.append("⚠ No rollback_image configured")
            if plan.risk_level != RiskLevel.HIGH:
                plan.risk_level = RiskLevel.MEDIUM

        # 스케일 변경 검증
        if plan.scale_change and plan.scale_change > 0:
            risk_reasons.append(f"Scale increase: {plan.scale_change} replicas added")

        plan.risk_reasons = risk_reasons
        return plan

    def assess_rollback(
        self,
        plan: DeploymentPlan,
        record: DeploymentRecord | None = None,
    ) -> RollbackAssessment:
        """
        배포 롤백 가능성 평가.

        §17.3 Rollback 가능 조건:
        - image_digest가 DeploymentRecord에 있는지
        - rollback_target이 설정되어 있는지

        불완전하면 risk_level high 격상 + risk_reasons에 명시

        인자:
            plan: DeploymentPlan
            record: DeploymentRecord (선택)

        반환:
            RollbackAssessment 객체
        """
        warnings: list[str] = []
        can_rollback = True
        reason = ""

        # 조건 1: rollback_target 설정
        if not plan.rollback_target:
            can_rollback = False
            warnings.append("rollback_target not configured")
            reason = "롤백 대상(이전 버전 이미지)이 설정되지 않았습니다"
        else:
            reason = f"Rollback to: {plan.rollback_target}"

        # 조건 2: image_digest (record가 있으면 검증)
        if record:
            if not record.image_digest:
                can_rollback = False
                warnings.append("Current deployment has no image_digest recorded")
                reason += " | Current image digest is missing"
            else:
                reason += f" | Current digest: {record.image_digest[:12]}..."

        # 조건 3: 배포 상태 확인
        if record and record.status != "succeeded":
            warnings.append(f"Last deployment status: {record.status} (not succeeded)")
            if record.status == "failed":
                can_rollback = False
                reason = "Previous deployment failed → cannot rollback"

        # 위험도 격상 (롤백 불가 → 높은 위험)
        if not can_rollback:
            plan.risk_level = RiskLevel.HIGH
            plan.risk_reasons = plan.risk_reasons or []
            plan.risk_reasons.extend([f"⚠ Rollback not possible: {reason}"])

        return RollbackAssessment(
            can_rollback=can_rollback,
            reason=reason,
            warnings=warnings,
        )

    def _is_remote_deployment(self, plan: DeploymentPlan) -> bool:
        """배포 대상이 원격인지 판단한다."""
        target = (plan.target or "").lower()
        # 로컬 키워드
        if any(x in target for x in ['localhost', '127.0.0.1', 'local', 'dev']):
            return False
        # 원격 키워드
        if any(x in target for x in ['prod', 'production', 'staging', 'cloud', 'aws', 'gcp', 'azure']):
            return True
        # 호스트명이나 IP가 있으면 원격으로 간주
        if target and target not in ['localhost', '127.0.0.1']:
            return True
        return False
>>>>>>> 74cf4369799da45d0fa49de67d56e58e01a2cc27
