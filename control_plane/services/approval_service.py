"""
Control Plane — Q2-B: Multi-Approver 승인 서비스

설계서 §Q2-B:
- 거부 사유 필수 입력
- 타임아웃 기본 24시간
- required_approvers 충족 시 approved 전환
- 1건이라도 거부 시 rejected 전환
- 모든 투표가 AuditLog에 추적
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.db.models import ApprovalRequest, ApprovalVote
from control_plane.models.schemas import (
    ApprovalRequestCreate,
    ApprovalRequestResponse,
    ApprovalStatus,
    ApprovalVoteRequest,
    ApprovalVoteResponse,
    AuditAction,
    AuditEventCreate,
)

logger = logging.getLogger(__name__)


class ApprovalService:

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # 요청 생성
    # ------------------------------------------------------------------

    async def create_request(
        self, request: ApprovalRequestCreate
    ) -> ApprovalRequestResponse:
        """
        OPA allow_with_approval 판정 시 Control Plane이 생성.
        승인자에게 표시할 정보: action 요약, 명령 미리보기, 리스크 사유,
        요청자, 만료 시각, 정책 번들 버전.
        """
        expires_at = datetime.now(timezone.utc) + timedelta(hours=request.expires_in_hours)

        ar = ApprovalRequest(
            org_id=request.org_id,
            requester_user_id=request.requester_user_id,
            requester_device_id=request.requester_device_id,
            action_summary=request.action_summary,
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            command_preview=request.command_preview,
            risk_reason=request.risk_reason,
            status=ApprovalStatus.PENDING.value,
            required_approvers=request.required_approvers,
            current_approvals=0,
            current_rejections=0,
            expires_at=expires_at,
            policy_bundle_version=request.policy_bundle_version,
            context=request.context,
        )
        self._db.add(ar)
        await self._db.flush()

        logger.info(
            "ApprovalRequest created: %s org=%s action=%s required=%d expires=%s",
            ar.approval_request_id, request.org_id, request.action_summary,
            request.required_approvers, expires_at.isoformat(),
        )
        return await self._to_response(ar)

    # ------------------------------------------------------------------
    # 투표
    # ------------------------------------------------------------------

    async def vote(
        self,
        approval_request_id: str,
        voter_user_id: str,
        vote: ApprovalVoteRequest,
        org_id: str,
    ) -> ApprovalRequestResponse:
        """
        승인자 투표.
        - 거부(rejected): 1건이라도 거부 즉시 전체 rejected
        - 승인(approved): required_approvers 충족 시 approved
        - 거부 사유는 필수 입력 (API 레이어에서도 검증)
        """
        ar = await self._get_request(approval_request_id, org_id)

        # 상태 확인
        if ar.status != ApprovalStatus.PENDING.value:
            raise ValueError(f"ApprovalRequest is already {ar.status}")

        # 만료 확인
        if ar.expires_at < datetime.now(timezone.utc):
            ar.status = ApprovalStatus.EXPIRED.value
            await self._db.flush()
            raise ValueError("ApprovalRequest has expired")

        # 중복 투표 확인
        existing = await self._db.execute(
            select(ApprovalVote).where(
                ApprovalVote.approval_request_id == approval_request_id,
                ApprovalVote.voter_user_id == voter_user_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError("Already voted on this request")

        # 거부 사유 필수
        if not vote.approved and not vote.reason.strip():
            raise ValueError("Rejection reason is required")

        # 투표 기록
        approval_vote = ApprovalVote(
            approval_request_id=approval_request_id,
            voter_user_id=voter_user_id,
            approved=vote.approved,
            reason=vote.reason,
        )
        self._db.add(approval_vote)

        # 카운터 업데이트
        if vote.approved:
            ar.current_approvals += 1
        else:
            ar.current_rejections += 1

        # 상태 전환 판단
        if ar.current_rejections >= 1:
            ar.status = ApprovalStatus.REJECTED.value
            logger.info("ApprovalRequest %s REJECTED by %s", approval_request_id, voter_user_id)
        elif ar.current_approvals >= ar.required_approvers:
            ar.status = ApprovalStatus.APPROVED.value
            logger.info(
                "ApprovalRequest %s APPROVED (%d/%d)",
                approval_request_id, ar.current_approvals, ar.required_approvers,
            )

        await self._db.flush()

        # AuditLog
        await self._record_audit(ar, voter_user_id, vote)

        return await self._to_response(ar)

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------

    async def get_request(
        self, approval_request_id: str, org_id: str
    ) -> ApprovalRequestResponse:
        ar = await self._get_request(approval_request_id, org_id)
        # 만료 자동 처리
        if ar.status == ApprovalStatus.PENDING.value and ar.expires_at < datetime.now(timezone.utc):
            ar.status = ApprovalStatus.EXPIRED.value
            await self._db.flush()
        return await self._to_response(ar)

    async def list_pending(self, org_id: str) -> list[ApprovalRequestResponse]:
        """승인 대기 중인 요청 목록. 만료된 것은 자동 처리."""
        result = await self._db.execute(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.org_id == org_id,
                ApprovalRequest.status == ApprovalStatus.PENDING.value,
            )
            .order_by(ApprovalRequest.created_at.desc())
        )
        now = datetime.now(timezone.utc)
        responses = []
        for ar in result.scalars().all():
            if ar.expires_at < now:
                ar.status = ApprovalStatus.EXPIRED.value
                await self._db.flush()
                continue
            responses.append(await self._to_response(ar))
        return responses

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_request(self, approval_request_id: str, org_id: str) -> ApprovalRequest:
        result = await self._db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.approval_request_id == approval_request_id,
                ApprovalRequest.org_id == org_id,
            )
        )
        ar = result.scalar_one_or_none()
        if ar is None:
            raise ValueError(f"ApprovalRequest {approval_request_id} not found")
        return ar

    async def _to_response(self, ar: ApprovalRequest) -> ApprovalRequestResponse:
        votes_result = await self._db.execute(
            select(ApprovalVote).where(ApprovalVote.approval_request_id == ar.approval_request_id)
        )
        votes = [
            ApprovalVoteResponse(
                vote_id=v.vote_id,
                approval_request_id=v.approval_request_id,
                voter_user_id=v.voter_user_id,
                approved=v.approved,
                reason=v.reason,
                voted_at=v.voted_at,
            )
            for v in votes_result.scalars().all()
        ]
        return ApprovalRequestResponse(
            approval_request_id=ar.approval_request_id,
            org_id=ar.org_id,
            requester_user_id=ar.requester_user_id,
            action_summary=ar.action_summary,
            resource_type=ar.resource_type,
            resource_id=ar.resource_id,
            command_preview=ar.command_preview,
            risk_reason=ar.risk_reason,
            status=ApprovalStatus(ar.status),
            required_approvers=ar.required_approvers,
            current_approvals=ar.current_approvals,
            current_rejections=ar.current_rejections,
            expires_at=ar.expires_at,
            policy_bundle_version=ar.policy_bundle_version,
            created_at=ar.created_at,
            votes=votes,
        )

    async def _record_audit(
        self, ar: ApprovalRequest, voter_user_id: str, vote: ApprovalVoteRequest
    ) -> None:
        from control_plane.services.audit import AuditService
        audit_svc = AuditService(self._db)
        action = (
            AuditAction.DEPLOYMENT_APPROVED if vote.approved
            else AuditAction.DEPLOYMENT_REJECTED
        )
        await audit_svc.record(
            org_id=ar.org_id,
            actor_user_id=voter_user_id,
            event=AuditEventCreate(
                action=action,
                resource_type=ar.resource_type,
                resource_id=ar.approval_request_id,
                after_state={
                    "approved": vote.approved,
                    "reason": vote.reason,
                    "approval_status": ar.status,
                    "approvals": ar.current_approvals,
                    "rejections": ar.current_rejections,
                },
                occurred_at=datetime.now(timezone.utc),
                policy_bundle_version=ar.policy_bundle_version,
            ),
        )

    # ------------------------------------------------------------------
    # escalate_to_security 알림 (설계서 §Q2-B "보안팀 에스컬레이션")
    # ------------------------------------------------------------------

    async def notify_security_team(
        self,
        org_id: str,
        actor_user_id: str,
        action: str,
        resource_type: str,
        resource_id: Optional[str],
        reason: str,
        policy_bundle_version: Optional[str] = None,
    ) -> None:
        """
        OPA 가 escalate_to_security 판정을 내렸을 때 호출.

        설계서 §Q2-B "보안팀 에스컬레이션":
          - AuditLog 기록
          - 보안 담당자 알림 (Slack/이메일 — 미연결 시 로그로 fail-soft)
        """
        from control_plane.services.audit import AuditService
        audit_svc = AuditService(self._db)
        # 1. AuditLog
        try:
            await audit_svc.record(
                org_id=org_id,
                actor_user_id=actor_user_id,
                event=AuditEventCreate(
                    action=AuditAction.SECURITY_ESCALATION,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    after_state={
                        "escalate_to_security": True,
                        "action": action,
                        "reason": reason,
                    },
                    occurred_at=datetime.now(timezone.utc),
                    policy_bundle_version=policy_bundle_version,
                ),
                is_suspicious=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("security escalation audit failed: %s", exc)

        # 2. 보안 담당자 알림 (Slack/이메일 webhook — 환경변수 미설정 시 로그로 fail-soft)
        import os
        webhook = os.environ.get("SECURITY_ESCALATION_WEBHOOK_URL")
        if not webhook:
            logger.warning(
                "[SECURITY ESCALATION] org=%s actor=%s action=%s resource=%s/%s reason=%s",
                org_id, actor_user_id, action, resource_type, resource_id, reason,
            )
            return
        try:
            import httpx  # 지연 import — 미설치 시 fail-soft
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    webhook,
                    json={
                        "text": (
                            f":rotating_light: ReCoder Security Escalation\n"
                            f"org={org_id} actor={actor_user_id}\n"
                            f"action={action} resource={resource_type}/{resource_id}\n"
                            f"reason={reason}"
                        )
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("security escalation webhook failed: %s", exc)
