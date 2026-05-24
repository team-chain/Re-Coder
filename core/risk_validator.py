"""
ReCoder Core — Risk Validator (v6.4 §17)

Proposal/Plan의 risk_level과 approval_level을 검증/재평가한다.

지원하는 검증 대상:
  - PatchProposal      (코드 패치)
  - InfraFileProposal  (Dockerfile / K8s 매니페스트 / GitHub Actions)
  - DeploymentPlan     (배포 계획)
  - ResponseProposal   (Ops 원격 조치)  ※ 존재 시

또한 §17.3 Rollback 가능 조건을 평가한다:
  - 이전 image_digest 존재 여부
  - rollback_target / rollback_image 설정 여부
  - DeploymentRecord 상태 / DB migration / 외부 리소스 변경 여부
불완전한 경우 risk_level을 high로 격상하고 risk_reasons에 사유를 기록한다.

추가 검증 규칙:
  - DB migration, S3/RDS 외부 리소스, IAM/Secret 변경, ECR lifecycle 삭제 등
    민감한 조건은 HIGH로 격상하고 approval_level 3 이상 강제.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema imports — defer to runtime to keep this module importable from both
# ``core.schemas`` and the flat ``schemas`` module layouts.
# ---------------------------------------------------------------------------

def _schemas():
    try:
        import schemas as _s  # type: ignore
    except ImportError:
        from core import schemas as _s  # type: ignore

    PatchProposal      = getattr(_s, "PatchProposal")
    InfraFileProposal  = getattr(_s, "InfraFileProposal")
    DeploymentPlan     = getattr(_s, "DeploymentPlan")
    DeploymentRecord   = getattr(_s, "DeploymentRecord")
    RiskLevel          = getattr(_s, "RiskLevel")
    ResponseProposal   = getattr(_s, "ResponseProposal", None)
    ActionType         = getattr(_s, "ActionType", None)
    DeployMethod       = getattr(_s, "DeployMethod", None)
    ApprovalLevel      = getattr(_s, "ApprovalLevel", None)
    return (
        PatchProposal, InfraFileProposal, DeploymentPlan, DeploymentRecord,
        RiskLevel, ResponseProposal, ActionType, DeployMethod, ApprovalLevel,
    )


# ---------------------------------------------------------------------------
# RollbackAssessment dataclass — public return type of assess_rollback()
# ---------------------------------------------------------------------------

@dataclass
class RollbackAssessment:
    """rollback 가능성 평가 결과 (§17.3)"""
    can_rollback: bool
    reason: str
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Risk-level ordering helpers
# ---------------------------------------------------------------------------

# Canonical schema only defines LOW/MEDIUM/HIGH. We still permit CRITICAL as a
# logical fourth level when the schema exposes it; otherwise it collapses to HIGH.
_RISK_ORDER_BASE = ["low", "medium", "high", "critical"]


def _risk_order(RiskLevel: Any) -> list[str]:
    """Return the ordered risk values supported by the active RiskLevel enum."""
    available = {m.value for m in RiskLevel}
    return [v for v in _RISK_ORDER_BASE if v in available]


def _escalate(current: Any, minimum: Any, RiskLevel: Any) -> Any:
    """Return max(current, minimum) according to the canonical risk order."""
    order = _risk_order(RiskLevel)
    curr_idx = order.index(current.value) if current.value in order else 0
    min_val = minimum.value if hasattr(minimum, "value") else str(minimum)
    if min_val not in order:
        # If asked for a level the enum doesn't support, cap at the highest.
        min_idx = len(order) - 1
    else:
        min_idx = order.index(min_val)
    return RiskLevel(order[max(curr_idx, min_idx)])


def _bump(current: Any, RiskLevel: Any) -> Any:
    """Raise *current* by one level (capped at the highest supported)."""
    order = _risk_order(RiskLevel)
    curr_idx = order.index(current.value) if current.value in order else 0
    next_idx = min(curr_idx + 1, len(order) - 1)
    return RiskLevel(order[next_idx])


# ---------------------------------------------------------------------------
# RiskValidator
# ---------------------------------------------------------------------------

class RiskValidator:
    """
    Stateless risk evaluation engine.

    각 ``validate_*`` 메서드는 proposal/plan 객체를 받아 risk_level,
    approval_level, risk_reasons를 재평가하고 (가능한 경우 in-place로 수정한)
    동일 객체를 반환한다.
    """

    # ------------------------------------------------------------------
    # PatchProposal validation
    # ------------------------------------------------------------------

    def validate_patch(self, proposal: Any) -> Any:
        """
        patch의 risk_level / approval_level / risk_reasons 재평가.

        규칙:
          - 패치 0개                → LOW
          - 단일 파일                → 기본 LOW
          - 2-3개 파일               → MEDIUM
          - 4개 이상                 → HIGH
          - 5개 초과                 → 추가 bump
          - 테스트 파일만             → LOW (강제)
          - migration/schema 파일    → HIGH
          - 의존성/lock 파일 변경     → ≥ MEDIUM
          - .env/secret/iam/auth 파일 → HIGH
          - 시스템 경로 (/etc, /sys)  → HIGH
          - 대량 삭제 (50줄+)         → HIGH
          - import/require 제거       → HIGH
          - 파일 삭제 패턴(diff 분석)  → 가능한 최고 수준
        """
        (
            PatchProposal, InfraFileProposal, DeploymentPlan, DeploymentRecord,
            RiskLevel, ResponseProposal, ActionType, DeployMethod, ApprovalLevel,
        ) = _schemas()

        reasons: list[str] = list(proposal.risk_reasons or [])

        if not proposal.patches:
            proposal.risk_level = RiskLevel.LOW
            proposal.risk_reasons = ["No patches to apply"]
            proposal.approval_level = self._approval_for_risk(
                RiskLevel.LOW, "patch", RiskLevel, ApprovalLevel
            )
            return proposal

        patch_count = len(proposal.patches)

        # 파일 개수 기반 기본 위험도
        if patch_count == 1:
            current_risk = RiskLevel.LOW
            reasons.append(f"Single file patch: {proposal.patches[0].file}")
        elif patch_count <= 3:
            current_risk = RiskLevel.MEDIUM
            reasons.append(f"Multiple files ({patch_count}): Medium risk")
        else:
            current_risk = RiskLevel.HIGH
            reasons.append(f"Multiple files ({patch_count}): Escalated to HIGH")

        # 패치 단위 패턴 분석
        for patch in proposal.patches:
            file_path = patch.file.lower()
            diff = patch.unified_diff or ""
            diff_lower = diff.lower()

            # Migration / schema 파일
            if re.search(r"migration|schema|alembic|flyway|liquibase", file_path):
                reasons.append(f"Database migration file: {patch.file}")
                current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)

            # 의존성/lock 파일
            if re.search(
                r"requirements.*\.txt|package-lock\.json|poetry\.lock|"
                r"pipfile\.lock|yarn\.lock|go\.sum",
                file_path,
            ):
                reasons.append(f"Dependency file modified: {patch.file}")
                current_risk = _escalate(current_risk, RiskLevel.MEDIUM, RiskLevel)

            # 보안 민감 파일
            if any(x in file_path for x in ("secret", "password", "token", "key",
                                             ".env", "credential", "auth", "iam")):
                reasons.append(f"⚠ Security-sensitive file: {patch.file}")
                current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)

            # 시스템 경로 / Dockerfile
            if any(x in file_path for x in ("/etc/", "/sys/", "/boot/", "dockerfile")):
                reasons.append(f"⚠ System file: {patch.file}")
                current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)

            # 대량 삭제
            deletion_count = diff.count("\n-")
            if deletion_count > 50:
                reasons.append(
                    f"⚠ Large deletion in {patch.file}: {deletion_count} lines removed"
                )
                current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)

            # 의존성 제거 패턴 (import/require 가 '-' 라인에 등장)
            removals = [
                line for line in diff.split("\n")
                if line.startswith("-") and not line.startswith("---")
            ]
            if any(
                "import" in line.lower() or "require" in line.lower()
                for line in removals
            ):
                reasons.append(f"⚠ Dependency removal detected in {patch.file}")
                current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)

            # 파일 삭제 (--- a/foo 만 있고 +++ b/foo 가 없는 경우)
            if re.search(r"^--- a/", diff, re.MULTILINE) and not re.search(
                r"^\+\+\+ b/", diff, re.MULTILINE
            ):
                reasons.append(f"File deletion detected: {patch.file}")
                current_risk = _escalate(
                    current_risk,
                    self._max_risk(RiskLevel),
                    RiskLevel,
                )

        # 5개 초과 시 한 단계 더 bump
        if patch_count > 5:
            reasons.append(f"Large patch set: {patch_count} files modified")
            current_risk = _bump(current_risk, RiskLevel)

        # 테스트 전용 변경 → LOW 강제
        all_test = bool(proposal.patches) and all(
            re.search(r"test_|_test\.|spec\.|\.spec\.|/tests?/", p.file.lower())
            for p in proposal.patches
        )
        if all_test:
            current_risk = RiskLevel.LOW
            reasons.append("Test-only changes")

        proposal.risk_level = current_risk
        proposal.risk_reasons = reasons
        proposal.approval_level = self._approval_for_risk(
            current_risk, "patch", RiskLevel, ApprovalLevel
        )
        return proposal

    # Backwards-compat alias.
    validate_patch_proposal = validate_patch

    # ------------------------------------------------------------------
    # InfraFileProposal validation
    # ------------------------------------------------------------------

    def validate_infra(self, proposal: Any) -> Any:
        """
        Dockerfile / Kubernetes 매니페스트 / GitHub Actions 검증.

        위험 패턴:
          - privileged 컨테이너 / Pod
          - host 네트워크/PID/IPC
          - hostPath / system directory mount (/etc, /sys 등)
          - root 사용자 / USER 지시어 누락
          - 버전 태그 없는 base image
          - GitHub Actions: 검증되지 않은 third-party action
        """
        (
            PatchProposal, InfraFileProposal, DeploymentPlan, DeploymentRecord,
            RiskLevel, ResponseProposal, ActionType, DeployMethod, ApprovalLevel,
        ) = _schemas()

        content = proposal.content or ""
        content_lower = content.lower()
        risk_reasons: list[str] = list(proposal.risk_reasons or [])
        detected_issues = 0

        file_type = str(proposal.file_type).lower() if proposal.file_type else ""

        # Dockerfile
        if "docker" in file_type and "compose" not in file_type:
            if "privileged=true" in content_lower or "--privileged" in content_lower:
                risk_reasons.append("⚠ Privileged container detected")
                detected_issues += 1

            if "network=host" in content_lower or "net=host" in content_lower:
                risk_reasons.append("⚠ Host network mode detected")
                detected_issues += 1

            # 명시적 USER 지시어 부재
            if "user root" in content_lower or "user " not in content_lower:
                risk_reasons.append("⚠ Container runs as root (no USER directive)")
                detected_issues += 1

            # 버전 없는 base image
            if "from " in content_lower:
                from_lines = [
                    line for line in content.split("\n")
                    if line.strip().lower().startswith("from ")
                ]
                for line in from_lines:
                    line_l = line.lower()
                    if ":" not in line and " as " not in line_l:
                        risk_reasons.append(
                            f"⚠ Unversioned base image: {line.strip()}"
                        )
                        detected_issues += 1

            # 시스템 디렉터리 mount
            if any(
                x in content_lower
                for x in ("mount_path /etc", "mount_path /sys", "mount_path /proc")
            ):
                risk_reasons.append("⚠ System directory mount detected")
                detected_issues += 1

        # Kubernetes
        elif "k8s" in file_type or "kubernetes" in file_type:
            if "privileged: true" in content_lower:
                risk_reasons.append("⚠ Privileged pod detected")
                detected_issues += 1

            if "hostpid: true" in content_lower or "hostipc: true" in content_lower:
                risk_reasons.append("⚠ Host PID/IPC access detected")
                detected_issues += 1

            if "hostnetwork: true" in content_lower:
                risk_reasons.append("⚠ Host network access detected")
                detected_issues += 1

            if "hostpath:" in content_lower:
                risk_reasons.append("⚠ HostPath volume detected")
                detected_issues += 1

        # docker-compose / GitHub Actions: lighter checks
        elif "compose" in file_type:
            if "privileged: true" in content_lower:
                risk_reasons.append("⚠ Privileged service in docker-compose")
                detected_issues += 1
            if "network_mode: host" in content_lower:
                risk_reasons.append("⚠ Host network in docker-compose")
                detected_issues += 1

        elif "github-actions" in file_type or "github_actions" in file_type:
            # third-party action 사용 (uses: someorg/...@vX)
            third_party = re.findall(
                r"uses:\s*([^\s/]+/[^\s@]+)@", content
            )
            trusted = {"actions", "aws-actions", "docker", "google-github-actions"}
            for ref in third_party:
                owner = ref.split("/")[0]
                if owner not in trusted:
                    risk_reasons.append(
                        f"⚠ Third-party GitHub Action used: {ref}"
                    )
                    detected_issues += 1

        # 위험도 결정
        if detected_issues >= 3:
            proposal.risk_level = RiskLevel.HIGH
        elif detected_issues >= 1:
            proposal.risk_level = RiskLevel.MEDIUM
        else:
            proposal.risk_level = RiskLevel.LOW

        proposal.risk_reasons = risk_reasons
        proposal.approval_level = self._approval_for_risk(
            proposal.risk_level, "infra", RiskLevel, ApprovalLevel
        )
        return proposal

    # ------------------------------------------------------------------
    # DeploymentPlan validation
    # ------------------------------------------------------------------

    def validate_deploy(
        self,
        plan: Any,
        last_record: Optional[Any] = None,
    ) -> Any:
        """
        배포 계획 검증.

        규칙:
          - 로컬 배포        → Level 1-2 가능
          - 원격 배포        → Level 3 강제
          - ECR push 등 레지스트리 변경 → 최소 HIGH + Level 3
          - env variable 변경 → HIGH + Level 3 (BLOCKED if available)
          - ECS / K8s 배포   → HIGH + Level 3
          - rollback_image / rollback_target 미설정 → 위험도 격상 및 경고
          - 스케일 변경 (scale_change > 0) → 정보성 사유 추가
        """
        (
            PatchProposal, InfraFileProposal, DeploymentPlan, DeploymentRecord,
            RiskLevel, ResponseProposal, ActionType, DeployMethod, ApprovalLevel,
        ) = _schemas()

        risk_reasons: list[str] = list(plan.risk_reasons or [])
        current_risk = plan.risk_level

        # 원격 배포 감지
        is_remote = self._is_remote_deployment(plan, DeployMethod)
        if is_remote:
            risk_reasons.append("Remote deployment detected → approval_level=3")
            current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
            plan.approval_level = self._max_approval(plan.approval_level, 3)

        # 배포 method 별 추가 규칙
        method_value = self._enum_value(plan.method)
        if method_value in ("ssh_direct", "ssh_docker"):
            risk_reasons.append("SSH remote deployment — direct server access")
            current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
            plan.approval_level = self._max_approval(plan.approval_level, 3)

        if method_value in ("ecr_ec2", "aws_ecs", "ecs", "k8s", "kubernetes"):
            risk_reasons.append("Cloud orchestration / registry deployment")
            current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
            plan.approval_level = self._max_approval(plan.approval_level, 3)

        # action 기반 규칙 — action은 schema에서 string
        action_value = self._enum_value(plan.action) if plan.action else ""
        action_lower = action_value.lower() if action_value else ""

        if "ecr_push" in action_lower:
            risk_reasons.append("ECR image push — immutable registry write")
            current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
            plan.approval_level = self._max_approval(plan.approval_level, 3)

        if "env_update" in action_lower or "ssh_env" in action_lower:
            risk_reasons.append("Environment variable modification")
            current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
            plan.approval_level = self._max_approval(plan.approval_level, 3)

        if "iam" in action_lower or "secret" in action_lower:
            risk_reasons.append("IAM / Secret manipulation")
            current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
            plan.approval_level = self._max_approval(plan.approval_level, 3)

        if "ecr_lifecycle" in action_lower or "lifecycle_delete" in action_lower:
            risk_reasons.append("ECR lifecycle deletion — irreversible image removal")
            current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
            plan.approval_level = self._max_approval(plan.approval_level, 3)

        # env 필드가 비어있지 않은 경우
        if getattr(plan, "env", None):
            if action_lower in ("ssh_env_update", "docker_run") or \
                    "env" in action_lower:
                risk_reasons.append("Environment variable modification")
                current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
                plan.approval_level = self._max_approval(plan.approval_level, 3)

        # rollback_image / rollback_target 검증
        rollback_image = getattr(plan, "rollback_image", "") or ""
        rollback_target = getattr(plan, "rollback_target", "") or ""
        if not rollback_image and not rollback_target:
            risk_reasons.append("⚠ No rollback image/target specified — cannot auto-revert")
            current_risk = _escalate(current_risk, RiskLevel.MEDIUM, RiskLevel)

        # 스케일 변경 (정보성)
        scale_change = getattr(plan, "scale_change", None)
        if scale_change and scale_change > 0:
            risk_reasons.append(f"Scale increase: {scale_change} replicas added")

        # last_record 정보 활용 (DB migration / 외부 리소스)
        if last_record is not None:
            data = self._record_as_dict(last_record)
            if data.get("db_migration_applied"):
                risk_reasons.append(
                    "Previous deployment applied DB migration — schema drift risk"
                )
                current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
            if data.get("external_resources_deleted"):
                risk_reasons.append(
                    "Previous deployment deleted external resources (S3/RDS) — destructive"
                )
                current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)

        plan.risk_level = current_risk
        plan.risk_reasons = risk_reasons

        # 최종 approval_level은 risk_level과 동기화 (상향만)
        derived = self._approval_for_risk(
            current_risk,
            action_lower or "deploy",
            RiskLevel,
            ApprovalLevel,
        )
        plan.approval_level = self._max_approval(plan.approval_level, derived)

        return plan

    # Backwards-compat alias.
    validate_deployment_plan = validate_deploy

    # ------------------------------------------------------------------
    # ResponseProposal validation (Ops)
    # ------------------------------------------------------------------

    def validate_response_proposal(
        self,
        proposal: Any,
        deployment_record: Optional[Any] = None,
    ) -> Any:
        """
        Ops ResponseProposal 검증.

        rollback 류 액션의 경우 deployment_record 부재 시 HIGH로 격상.
        """
        (
            PatchProposal, InfraFileProposal, DeploymentPlan, DeploymentRecord,
            RiskLevel, ResponseProposal, ActionType, DeployMethod, ApprovalLevel,
        ) = _schemas()

        reasons: list[str] = list(getattr(proposal, "risk_reasons", []) or [])
        current_risk = getattr(proposal, "risk_level", RiskLevel.LOW)

        action_value = self._enum_value(getattr(proposal, "action_type", ""))
        is_rollback_action = bool(
            re.search(r"rollback|restart|redeploy", action_value or "", re.IGNORECASE)
        )

        if is_rollback_action:
            deployment_id = ""
            if deployment_record is not None:
                deployment_id = getattr(deployment_record, "deployment_id", "") or ""
            feasible, warnings = self.check_rollback_feasibility(deployment_id)
            if not feasible:
                reasons.extend(warnings)
                current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)
                reasons.append(
                    "Rollback feasibility not confirmed — manual review required"
                )

        if deployment_record is None and is_rollback_action:
            reasons.append("No deployment record found — rollback target unknown")
            current_risk = _escalate(current_risk, RiskLevel.HIGH, RiskLevel)

        proposal.risk_level = current_risk
        proposal.risk_reasons = reasons
        proposal.approval_level = self._approval_for_risk(
            current_risk, action_value or "response", RiskLevel, ApprovalLevel
        )
        return proposal

    # ------------------------------------------------------------------
    # Rollback assessment / feasibility
    # ------------------------------------------------------------------

    def assess_rollback(
        self,
        plan: Any,
        record: Optional[Any] = None,
    ) -> RollbackAssessment:
        """
        배포 롤백 가능성 평가 (§17.3).

        평가 조건:
          - rollback_target (또는 rollback_image) 설정 여부
          - DeploymentRecord에 image_digest 기록 여부
          - 마지막 배포 status가 succeeded/deployed 인지
          - env snapshot, DB migration, 외부 리소스 변경 여부

        불완전한 경우 plan.risk_level을 HIGH로 격상하고 risk_reasons에 사유 추가.
        """
        (
            PatchProposal, InfraFileProposal, DeploymentPlan, DeploymentRecord,
            RiskLevel, ResponseProposal, ActionType, DeployMethod, ApprovalLevel,
        ) = _schemas()

        warnings: list[str] = []
        can_rollback = True
        reason_parts: list[str] = []

        rollback_target = getattr(plan, "rollback_target", "") or ""
        rollback_image = getattr(plan, "rollback_image", "") or ""

        # 조건 1: rollback_target / rollback_image
        if not rollback_target and not rollback_image:
            can_rollback = False
            warnings.append("rollback_target/rollback_image not configured")
            reason_parts.append(
                "롤백 대상(이전 버전 이미지)이 설정되지 않았습니다"
            )
        else:
            reason_parts.append(
                f"Rollback to: {rollback_target or rollback_image}"
            )

        # 조건 2: image_digest (record 기준)
        if record is not None:
            image_digest = getattr(record, "image_digest", "") or ""
            if not image_digest:
                can_rollback = False
                warnings.append("Current deployment has no image_digest recorded")
                reason_parts.append("Current image digest is missing")
            else:
                reason_parts.append(f"Current digest: {image_digest[:12]}...")

            # 조건 3: 배포 상태
            status_value = self._enum_value(getattr(record, "status", ""))
            if status_value and status_value not in ("succeeded", "deployed"):
                warnings.append(
                    f"Last deployment status: {status_value} (not succeeded/deployed)"
                )
                if status_value == "failed":
                    can_rollback = False
                    reason_parts = ["Previous deployment failed → cannot rollback"]

            # 조건 4: env snapshot
            data = self._record_as_dict(record)
            if not data.get("env_snapshot") and not data.get("env"):
                warnings.append("env snapshot not recorded — config drift risk")

            # 조건 5: DB migration / 외부 리소스
            if data.get("db_migration_applied"):
                warnings.append(
                    "Database migration was applied — rollback may cause schema mismatch"
                )
            if data.get("external_resources_deleted"):
                warnings.append(
                    "External resources were deleted — rollback is destructive"
                )

        # 위험도 격상
        if not can_rollback:
            plan.risk_level = _escalate(
                getattr(plan, "risk_level", RiskLevel.LOW),
                RiskLevel.HIGH,
                RiskLevel,
            )
            plan.risk_reasons = list(plan.risk_reasons or []) + [
                f"⚠ Rollback not possible: {' | '.join(reason_parts)}"
            ]
        elif warnings:
            # 경고는 있지만 롤백은 가능 — risk_reasons에 정보 추가
            plan.risk_reasons = list(plan.risk_reasons or []) + [
                f"Rollback warning: {w}" for w in warnings
            ]

        return RollbackAssessment(
            can_rollback=can_rollback,
            reason=" | ".join(reason_parts),
            warnings=warnings,
        )

    def check_rollback_feasibility(
        self,
        deployment_id: str,
    ) -> tuple[bool, list[str]]:
        """
        Return (is_feasible, warning_messages).

        ~/.recoder/deployments/{deployment_id}.json 을 읽어:
          - rollback_target/rollback_image 존재 여부
          - DB migration 적용 여부
          - 외부 리소스 삭제 여부
          - image_digest 존재 여부
        를 검증한다.
        """
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

        # rollback target
        if not data.get("rollback_target") and not data.get("rollback_image"):
            warnings.append("No rollback image tag recorded")
            return False, warnings

        # image_digest
        if not data.get("image_digest"):
            warnings.append("No image_digest recorded for current deployment")

        # DB migration
        if data.get("db_migration_applied"):
            warnings.append(
                "Database migration was applied — rollback may cause schema mismatch"
            )

        # 외부 리소스 삭제
        if data.get("external_resources_deleted"):
            warnings.append(
                "External resources were deleted during deployment — rollback is destructive"
            )

        # env snapshot
        if not data.get("env_snapshot") and not data.get("env"):
            warnings.append("env snapshot not recorded — config drift risk")

        # 경고가 없으면 feasible
        return len(warnings) == 0, warnings

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _approval_for_risk(
        self,
        risk_level: Any,
        action_type: str,
        RiskLevel: Any,
        ApprovalLevel: Any = None,
    ) -> Any:
        """
        risk_level + action_type → approval_level 매핑.

        Level 1 (AUTO)           : LOW risk, 로컬 파일 수정
        Level 2 (CONFIRM)        : MEDIUM risk, 로컬 명령 실행
        Level 3 (DOUBLE_CONFIRM) : HIGH risk, 원격 인프라 변경
        Level 4 (BLOCKED)        : env/secret/IAM/ECR push 등 민감 설정 변경

        반환 타입: ApprovalLevel enum 이 있으면 enum, 아니면 int (canonical schema).
        """
        action_str = str(action_type or "")

        # 민감 액션 — Level 4 (또는 Level 3 if no BLOCKED in enum)
        sensitive = re.search(
            r"env_update|ecr_push|iam|secret|ssh_env|ecr_lifecycle",
            action_str,
            re.IGNORECASE,
        )
        if sensitive:
            return self._to_approval(4, ApprovalLevel)

        # 원격 인프라
        remote = re.search(
            r"ssh_docker|ssh_direct|ecs|k8s|kubernetes|scale_up|scale_down|ecr_ec2",
            action_str,
            re.IGNORECASE,
        )
        if remote or risk_level == RiskLevel.HIGH:
            return self._to_approval(3, ApprovalLevel)

        # risk_level 기반 기본 매핑
        risk_value = self._enum_value(risk_level)
        mapping = {
            "low":      1,
            "medium":   2,
            "high":     3,
            "critical": 4,
        }
        level_int = mapping.get(risk_value, 2)
        return self._to_approval(level_int, ApprovalLevel)

    @staticmethod
    def _to_approval(level_int: int, ApprovalLevel: Any) -> Any:
        """Convert an integer 1..4 to ApprovalLevel enum if available, else int."""
        if ApprovalLevel is None:
            return level_int
        try:
            # Enum may be int-based or have named members.
            return ApprovalLevel(level_int)
        except Exception:
            # Try by attribute name conventions.
            name_map = {
                1: "AUTO",
                2: "CONFIRM",
                3: "DOUBLE_CONFIRM",
                4: "BLOCKED",
            }
            name = name_map.get(level_int)
            if name and hasattr(ApprovalLevel, name):
                return getattr(ApprovalLevel, name)
            return level_int

    @staticmethod
    def _max_approval(current: Any, new: Any) -> Any:
        """Return the stricter (higher) of two approval levels."""
        def _to_int(x: Any) -> int:
            if x is None:
                return 0
            if isinstance(x, int):
                return x
            if hasattr(x, "value"):
                v = x.value
                if isinstance(v, int):
                    return v
                try:
                    return int(v)
                except Exception:
                    return 0
            try:
                return int(x)
            except Exception:
                return 0

        return current if _to_int(current) >= _to_int(new) else new

    def _is_remote_deployment(self, plan: Any, DeployMethod: Any) -> bool:
        """배포 대상이 원격인지 판단한다."""
        # method 기반 판단
        if DeployMethod is not None:
            method_value = self._enum_value(getattr(plan, "method", ""))
            remote_methods = {
                "ssh_direct", "ssh_docker", "ecr_ec2", "aws_ecs",
                "ecs", "k8s", "kubernetes",
            }
            if method_value in remote_methods:
                return True
            if method_value in ("local_docker", "local"):
                return False

        # target 문자열 기반 판단 (legacy 필드)
        target = (getattr(plan, "target", "") or "").lower()
        if any(x in target for x in ("localhost", "127.0.0.1", "local", "dev")):
            return False
        if any(
            x in target
            for x in ("prod", "production", "staging", "cloud", "aws", "gcp", "azure")
        ):
            return True
        if target and target not in ("localhost", "127.0.0.1"):
            return True
        return False

    @staticmethod
    def _enum_value(x: Any) -> str:
        """Return the underlying string value for an Enum or pass-through string."""
        if x is None:
            return ""
        if hasattr(x, "value"):
            v = x.value
            return str(v) if v is not None else ""
        return str(x)

    @staticmethod
    def _max_risk(RiskLevel: Any) -> Any:
        """Return the highest available RiskLevel enum member."""
        if hasattr(RiskLevel, "CRITICAL"):
            return RiskLevel.CRITICAL
        return RiskLevel.HIGH

    @staticmethod
    def _record_as_dict(record: Any) -> dict:
        """Convert a DeploymentRecord (dataclass / Pydantic / dict) to a dict."""
        if record is None:
            return {}
        if isinstance(record, dict):
            return record
        if hasattr(record, "to_dict"):
            try:
                return record.to_dict()
            except Exception:
                pass
        if hasattr(record, "model_dump"):
            try:
                return record.model_dump()
            except Exception:
                pass
        if hasattr(record, "__dict__"):
            return dict(record.__dict__)
        return {}
