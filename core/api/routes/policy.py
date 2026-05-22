"""
Local Core — 정책 평가 라우트

Extension이 작업 실행 전 이 엔드포인트를 호출한다.
Local Core가 OPA에 질의하고 결과를 반환한다.

- POST /api/policy/evaluate     — 정책 평가 (로컬 OPA)
- GET  /api/policy/cache/status — 캐시 상태 확인
- POST /api/policy/cache/reload — 정책 캐시 강제 갱신
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.opa_client import OPAResult, opa_client
from core.policy_cache import policy_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/policy", tags=["policy"])

_CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://localhost:18000")


class PolicyEvaluateRequest(BaseModel):
    action: str               # e.g. "deployment:request"
    level: int = Field(ge=1, le=4)
    resource_type: str = ""
    resource_id: Optional[str] = None
    org_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class PolicyEvaluateResponse(BaseModel):
    decision: str             # allow / allow_with_approval / deny / deny_with_fix_suggestion / escalate_to_security
    reason: str
    fix_suggestion: Optional[str] = None
    required_approvers: int = 0
    approval_request_id: Optional[str] = None
    policy_bundle_version: Optional[str] = None
    opa_available: bool = True

    @property
    def is_blocked(self) -> bool:
        return self.decision in ("deny", "deny_with_fix_suggestion", "escalate_to_security")

    @property
    def needs_approval(self) -> bool:
        return self.decision == "allow_with_approval"


@router.post("/evaluate", response_model=PolicyEvaluateResponse)
async def evaluate(request: PolicyEvaluateRequest) -> PolicyEvaluateResponse:
    """
    Local Core OPA 정책 평가.

    흐름:
    1. policy_cache에서 현재 bundle_version 확인
    2. OPA에 평가 요청 (fail-closed)
    3. allow_with_approval이면 Control Plane에 ApprovalRequest 생성 요청
    """
    cached_version = policy_cache.get_cached_version()

    result: OPAResult = await opa_client.evaluate(
        action=request.action,
        level=request.level,
        context=request.context,
        org_id=request.org_id,
        resource_type=request.resource_type,
        resource_id=request.resource_id,
        policy_bundle_version=cached_version or "unknown",
    )

    approval_request_id: Optional[str] = None

    # allow_with_approval → Control Plane에 ApprovalRequest 생성 요청
    if result.decision == "allow_with_approval" and request.org_id:
        approval_request_id = await _request_approval(request, result, cached_version)

    return PolicyEvaluateResponse(
        decision=result.decision,
        reason=result.reason,
        fix_suggestion=result.fix_suggestion,
        required_approvers=result.required_approvers,
        approval_request_id=approval_request_id,
        policy_bundle_version=cached_version,
        opa_available=result.opa_available,
    )


@router.get("/cache/status")
async def cache_status() -> dict:
    """현재 정책 캐시 상태"""
    return {
        "bundle_version": policy_cache.get_cached_version(),
        "is_valid": policy_cache.is_valid(),
        "opa_available": await opa_client.health_check(),
    }


@router.post("/cache/reload")
async def reload_cache(device_token: str, org_id: str) -> dict:
    """정책 캐시 강제 갱신 (Control Plane에서 최신 버전 다운로드)"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{_CONTROL_PLANE_URL}/policy/{org_id}/bundles/active",
                headers={"Authorization": f"Bearer {device_token}"},
            )
            resp.raise_for_status()
            latest_version = resp.json().get("version")

        ok = await policy_cache.ensure_fresh(device_token, org_id, latest_version)
        if ok:
            opa_url = os.environ.get("OPA_URL", "http://localhost:8181")
            await policy_cache.load_to_opa(opa_url)

        return {
            "success": ok,
            "bundle_version": policy_cache.get_cached_version(),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


async def _request_approval(
    request: PolicyEvaluateRequest,
    result: OPAResult,
    bundle_version: Optional[str],
) -> Optional[str]:
    """Control Plane에 ApprovalRequest 생성 요청. 실패해도 평가 결과는 반환."""
    from core.singleton import read_runtime_json
    runtime = read_runtime_json()
    device_token = runtime.get("device_token")
    if not device_token or not request.org_id:
        return None

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{_CONTROL_PLANE_URL}/policy/{request.org_id}/evaluate",
                json={
                    "action": request.action,
                    "resource_type": request.resource_type,
                    "resource_id": request.resource_id,
                    "context": request.context,
                    "level": request.level,
                    "policy_bundle_version": bundle_version,
                },
                headers={"Authorization": f"Bearer {device_token}"},
            )
            if resp.status_code == 200:
                return resp.json().get("approval_request_id")
    except Exception as exc:
        logger.warning("Failed to create ApprovalRequest on Control Plane: %s", exc)
    return None
