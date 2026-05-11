"""
Risk Validator (v6.4 §17).

Proposal의 risk_level을 검증하고 rollback 가능성을 평가한다.
- PatchProposal의 위험도 재평가 (다중 파일 수정 시 medium 이상)
- InfraFileProposal 검증 (privileged/host mount 감지 시 high 격상)
- DeploymentPlan 검증 (원격 배포 시 Level 3 강제)
- Rollback 가능 조건 체크
"""

from __future__ import annotations

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
