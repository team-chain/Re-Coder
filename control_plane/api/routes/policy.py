"""
Control Plane — Q2-B: PolicyBundle + OPA 평가 라우트

- GET  /policy/{org_id}/bundles           — Bundle 목록
- POST /policy/{org_id}/bundles           — Bundle 생성 (Preset 설정)
- GET  /policy/{org_id}/bundles/active    — 현재 활성 Bundle
- GET  /policy/{org_id}/bundles/{ver}/rego — Rego 다운로드 (Local Core용)
- POST /policy/{org_id}/evaluate          — OPA 평가 (Local Core → Control Plane)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.middleware.device_auth import (
    DeviceContext,
    get_current_device,
    require_permission_dep,
)
from control_plane.db.session import get_db
from control_plane.models.schemas import (
    AuditAction,
    AuditEventCreate,
    OPADecisionStatus,
    OPAEvaluateRequest,
    OPAEvaluateResponse,
    Permission,
    PolicyBundleCreate,
    PolicyBundleResponse,
)
from control_plane.services.approval_service import ApprovalService
from control_plane.services.audit import AuditService
from control_plane.services.org_service import OrgService
from control_plane.services.policy_service import PolicyService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/policy", tags=["policy"])


# ---------------------------------------------------------------------------
# PolicyBundle CRUD
# ---------------------------------------------------------------------------

@router.post(
    "/{org_id}/bundles",
    response_model=PolicyBundleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission_dep("policy:write"))],
)
async def create_bundle(
    org_id: str,
    request: PolicyBundleCreate,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> PolicyBundleResponse:
    """Preset 설정으로 PolicyBundle 생성 + Rego 자동 생성."""
    _assert_org(ctx, org_id)
    request.org_id = org_id
    svc = PolicyService(db)
    bundle = await svc.create_bundle(request, creator_user_id=ctx.user_id)

    # AuditLog
    audit_svc = AuditService(db)
    import datetime as _dt
    await audit_svc.record(
        org_id=org_id,
        actor_user_id=ctx.user_id,
        event=AuditEventCreate(
            action=AuditAction.POLICY_BUNDLE_UPDATED,
            resource_type="policy_bundle",
            resource_id=bundle.bundle_id,
            after_state={"version": bundle.version, "sha256": bundle.sha256},
            occurred_at=_dt.datetime.now(_dt.timezone.utc),
            policy_bundle_version=bundle.version,
        ),
        actor_device_id=ctx.device_id,
    )
    return bundle


@router.get(
    "/{org_id}/bundles",
    response_model=list[PolicyBundleResponse],
    dependencies=[Depends(require_permission_dep("policy:read"))],
)
async def list_bundles(
    org_id: str,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> list[PolicyBundleResponse]:
    _assert_org(ctx, org_id)
    svc = PolicyService(db)
    return await svc.list_bundles(org_id)


@router.get(
    "/{org_id}/bundles/active",
    response_model=PolicyBundleResponse,
    dependencies=[Depends(require_permission_dep("policy:read"))],
)
async def get_active_bundle(
    org_id: str,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> PolicyBundleResponse:
    _assert_org(ctx, org_id)
    svc = PolicyService(db)
    bundle = await svc.get_active_bundle(org_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="No active policy bundle found")
    return bundle


@router.get("/{org_id}/bundles/{version}/rego", response_class=PlainTextResponse)
async def download_rego(
    org_id: str,
    version: str,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> str:
    """
    Local Core가 PolicyBundle Rego를 다운로드한다.
    응답 헤더에 X-SHA256를 포함해 Local Core가 sha256 검증에 사용한다.
    """
    _assert_org(ctx, org_id)
    svc = PolicyService(db)
    result = await svc.get_bundle_rego(org_id, version)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Policy bundle {version} not found")
    rego_content, sha256 = result
    from fastapi.responses import Response
    return Response(
        content=rego_content,
        media_type="text/plain",
        headers={"X-SHA256": sha256, "X-Version": version},
    )


# ---------------------------------------------------------------------------
# OPA 평가 (Local Core → Control Plane → OPA)
# ---------------------------------------------------------------------------

@router.post("/{org_id}/evaluate", response_model=OPAEvaluateResponse)
async def evaluate_policy(
    org_id: str,
    request: OPAEvaluateRequest,
    ctx: DeviceContext = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
) -> OPAEvaluateResponse:
    """
    Local Core가 작업 실행 전 정책 평가를 요청한다.

    OPA unavailable → fail-closed: Level 3~4는 deny 반환.
    allow_with_approval → ApprovalRequest 자동 생성.
    """
    _assert_org(ctx, org_id)

    # 활성 PolicyBundle 조회
    policy_svc = PolicyService(db)
    bundle = await policy_svc.get_active_bundle(org_id)
    bundle_version = bundle.version if bundle else "none"

    # OPA 평가 수행
    decision, reason, fix_suggestion, required_approvers = await _evaluate_opa(
        org_id=org_id,
        bundle_version=bundle_version,
        bundle_rego=bundle.sha256 if bundle else None,  # OPA URL은 환경변수로
        request=request,
    )

    approval_request_id: str | None = None

    # allow_with_approval → ApprovalRequest 생성
    if decision == OPADecisionStatus.ALLOW_WITH_APPROVAL:
        from control_plane.models.schemas import ApprovalRequestCreate
        approval_svc = ApprovalService(db)
        ar = await approval_svc.create_request(
            ApprovalRequestCreate(
                org_id=org_id,
                requester_user_id=ctx.user_id,
                requester_device_id=ctx.device_id,
                action_summary=f"{request.action} on {request.resource_type}",
                resource_type=request.resource_type,
                resource_id=request.resource_id,
                risk_reason=reason,
                required_approvers=required_approvers,
                policy_bundle_version=bundle_version,
                context=request.context,
            )
        )
        approval_request_id = ar.approval_request_id

    # AuditLog
    audit_svc = AuditService(db)
    import datetime as _dt
    await audit_svc.record(
        org_id=org_id,
        actor_user_id=ctx.user_id,
        event=AuditEventCreate(
            action=AuditAction.POLICY_EVALUATED,
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            after_state={
                "decision": decision.value,
                "reason": reason,
                "action": request.action,
                "level": request.level,
            },
            occurred_at=_dt.datetime.now(_dt.timezone.utc),
            policy_bundle_version=bundle_version,
        ),
        actor_device_id=ctx.device_id,
    )

    return OPAEvaluateResponse(
        decision=decision,
        reason=reason,
        fix_suggestion=fix_suggestion,
        required_approvers=required_approvers,
        approval_request_id=approval_request_id,
        policy_bundle_version=bundle_version,
    )


# ---------------------------------------------------------------------------
# OPA REST API 호출 (fail-closed)
# ---------------------------------------------------------------------------

import os
import httpx

_OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181")
_OPA_POLICY_PATH = "/v1/data/recoder/policy"


async def _evaluate_opa(
    org_id: str,
    bundle_version: str,
    bundle_rego: str | None,
    request: OPAEvaluateRequest,
) -> tuple[OPADecisionStatus, str, str | None, int]:
    """
    OPA REST API 호출 → (decision, reason, fix_suggestion, required_approvers).
    OPA unavailable 시 fail-closed (Level 3~4 → deny).
    """
    input_data = {
        "input": {
            "org_id": org_id,
            "action": request.action,
            "resource_type": request.resource_type,
            "resource_id": request.resource_id,
            "level": request.level,
            "context": request.context,
            "policy_bundle_version": bundle_version,
        }
    }

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(f"{_OPA_URL}{_OPA_POLICY_PATH}", json=input_data)
            resp.raise_for_status()
            result = resp.json().get("result", {})

        decision_str = result.get("decision", "deny")
        decision = OPADecisionStatus(decision_str)
        deny_reasons: list[str] = list(result.get("deny_reasons", []))
        reason = "; ".join(deny_reasons) if deny_reasons else _default_reason(decision)
        fix_suggestion = _fix_suggestion(deny_reasons)
        required_approvers = int(result.get("required_approvers", 0))

        return decision, reason, fix_suggestion, required_approvers

    except httpx.HTTPError as exc:
        logger.error("OPA unavailable: %s — fail-closed applied", exc)
        # fail-closed: Level 3~4 차단
        if request.level >= 3:
            return (
                OPADecisionStatus.DENY,
                "OPA 정책 서버에 연결할 수 없습니다. Level 3~4 작업은 차단됩니다 (fail-closed).",
                None,
                0,
            )
        # Level 1~2는 allow (로컬 작업 허용)
        return OPADecisionStatus.ALLOW, "OPA 오프라인 — Level 1~2 로컬 작업 허용", None, 0

    except Exception as exc:
        logger.error("OPA evaluation error: %s", exc)
        return (
            OPADecisionStatus.DENY,
            f"정책 평가 중 오류 발생: {exc}",
            None,
            0,
        )


def _default_reason(decision: OPADecisionStatus) -> str:
    return {
        OPADecisionStatus.ALLOW: "정책 검사 통과",
        OPADecisionStatus.ALLOW_WITH_APPROVAL: "승인이 필요한 작업입니다",
        OPADecisionStatus.DENY: "정책에 의해 차단됩니다",
        OPADecisionStatus.DENY_WITH_FIX_SUGGESTION: "정책 위반 — 수정 가이드를 확인하세요",
        OPADecisionStatus.ESCALATE_TO_SECURITY: "보안팀 에스컬레이션이 필요합니다",
    }.get(decision, "알 수 없는 결정")


def _fix_suggestion(deny_reasons: list[str]) -> str | None:
    for r in deny_reasons:
        if "trivy_critical_block" in r:
            return "이미지에 CRITICAL 취약점이 있습니다. `trivy image <image>` 를 실행해 취약한 패키지를 업데이트하세요."
        if "port_22_block" in r:
            return "22번 포트를 0.0.0.0/0으로 열지 마세요. Security Group에서 특정 IP만 허용하세요."
        if "prod_main_branch_only" in r:
            return "프로덕션 배포는 main 브랜치에서만 가능합니다. 현재 브랜치를 main에 머지한 후 다시 시도하세요."
    return None


def _assert_org(ctx: DeviceContext, org_id: str) -> None:
    if ctx.org_id != org_id:
        raise HTTPException(status_code=403, detail="Access to this organization is not permitted")
