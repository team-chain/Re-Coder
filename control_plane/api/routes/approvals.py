"""
Control Plane — Q2-B: Multi-Approver 승인 라우트

설계서 §Q2-B:
- Web UI 기반 2인 승인 (Must)
- 거부 사유 필수
- 승인자에게 표시: action 요약, 명령 미리보기, 리스크 사유, 요청자, 만료 시각, 정책 버전
- 모든 투표가 AuditLog에 추적

- GET  /approvals/{org_id}/pending          — 승인 대기 목록
- GET  /approvals/{org_id}/{id}             — 요청 상세
- POST /approvals/{org_id}/{id}/vote        — 승인 / 거부
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.middleware.device_auth import (
    DeviceContext,
    get_current_device,
    require_permission_dep,
)
from control_plane.db.session import get_db
from control_plane.models.schemas import (
    ApprovalRequestResponse,
    ApprovalVoteRequest,
)
from control_plane.services.approval_service import ApprovalService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get(
    "/{org_id}/pending",
    response_model=list[ApprovalRequestResponse],
    dependencies=[Depends(require_permission_dep("deployment:approve"))],
)
async def list_pending(
    org_id: str,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> list[ApprovalRequestResponse]:
    """
    승인 대기 중인 요청 목록.
    deployment:approve 권한 필요 (approver/owner/admin).
    """
    _assert_org(ctx, org_id)
    svc = ApprovalService(db)
    return await svc.list_pending(org_id)


@router.get(
    "/{org_id}/{approval_request_id}",
    response_model=ApprovalRequestResponse,
    dependencies=[Depends(require_permission_dep("deployment:approve"))],
)
async def get_approval(
    org_id: str,
    approval_request_id: str,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> ApprovalRequestResponse:
    """
    승인 요청 상세 조회.
    승인자에게 표시하는 정보:
      - action_summary: 무슨 작업인지
      - command_preview: 실행될 명령 미리보기
      - risk_reason: 리스크 사유
      - requester_user_id: 요청자
      - expires_at: 만료 시각
      - policy_bundle_version: 어떤 정책 버전으로 판단됐는지
      - votes: 현재까지 투표 현황
    """
    _assert_org(ctx, org_id)
    svc = ApprovalService(db)
    try:
        return await svc.get_request(approval_request_id, org_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post(
    "/{org_id}/{approval_request_id}/vote",
    response_model=ApprovalRequestResponse,
    dependencies=[Depends(require_permission_dep("deployment:approve"))],
)
async def vote(
    org_id: str,
    approval_request_id: str,
    vote_request: ApprovalVoteRequest,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> ApprovalRequestResponse:
    """
    승인 또는 거부 투표.

    규칙:
    - 거부 시 reason 필수 입력
    - 자신의 요청에는 투표 불가 (approval_service에서 강제하지 않으므로 여기서 처리)
    - 1건이라도 거부 시 rejected 전환
    - required_approvers 충족 시 approved 전환
    - 모든 투표가 AuditLog에 기록됨
    """
    _assert_org(ctx, org_id)

    # 자기 자신 승인 방지 — 요청자가 본인이면 투표 불가
    svc = ApprovalService(db)
    try:
        ar = await svc.get_request(approval_request_id, org_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if ar.requester_user_id == ctx.user_id:
        raise HTTPException(
            status_code=403,
            detail="자신이 요청한 작업에는 직접 투표할 수 없습니다",
        )

    if not vote_request.approved and not vote_request.reason.strip():
        raise HTTPException(
            status_code=422,
            detail="거부 시 reason은 필수 입력입니다",
        )

    try:
        return await svc.vote(
            approval_request_id=approval_request_id,
            voter_user_id=ctx.user_id,
            vote=vote_request,
            org_id=org_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _assert_org(ctx: DeviceContext, org_id: str) -> None:
    if ctx.org_id != org_id:
        raise HTTPException(status_code=403, detail="Access to this organization is not permitted")
