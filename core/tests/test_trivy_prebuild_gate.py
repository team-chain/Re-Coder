"""빌드 전 이미지를 Trivy 에 넘기던 문제 — 보드 이슈
「Trivy 가 빌드 전 이미지를 스캔 시도 — 보안 스캔이 한 번도 안 돈 채 통과」.

무엇이 사고였나
    플랜 시점에는 이미지가 아직 빌드되지 않았다. 없는 이미지를 Trivy 에 넘기면
    status=error 가 되는데, 게이트는 status=="ok" 만 읽어서 error 를 **조용히
    무시**했다. 스캔이 한 번도 안 돌았는데 blockers 도 risk_reasons 도 비어
    있으니, 화면에서는 검사를 통과한 배포처럼 보였다.

여기서 고정하는 것
    1. 미빌드 이미지는 스캔하지 않는다 — 대신 unverified 로 남는다.
    2. 스캔 error 도 통과가 아니다 — 사유가 risk_reasons 로 올라온다.
    3. unverified 플랜은 승인 강도가 이중 확인으로 올라가고, 실행 시점에
       (이미지가 생긴 뒤) 스캔이 실제로 1회 돈다. CRITICAL 이면 실행 전 차단.
    4. [음성 대조] 정상적으로 스캔이 돈 플랜은 아무것도 달라지지 않는다.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from api.routes import deploy  # noqa: E402
from schemas import (  # noqa: E402
    ActionType,
    ApprovalLevel,
    DeployMethod,
    DeploymentPlan,
    RiskLevel,
)


def _plan_request(image="recoder-app:v1"):
    return deploy.DeployPlanRequest(workspace_path="/tmp/ws", image=image)


def _fake_plan(image="recoder-app:v1"):
    return DeploymentPlan(
        method=DeployMethod.LOCAL_DOCKER,
        action=ActionType.DOCKER_RUN,
        image=image,
        container_name="recoder-app",
        ports={"8000": "8000"},
        risk_level=RiskLevel.LOW,
        approval_level=ApprovalLevel.CONFIRM,
    )


# ---------------------------------------------------------------------------
# 1. 게이트 — 미빌드 이미지
# ---------------------------------------------------------------------------


def test_미빌드_이미지는_스캔하지_않고_unverified_로_남는다(monkeypatch) -> None:
    monkeypatch.setattr(deploy, "_local_image_exists", lambda image: False)

    async def _must_not_scan_trivy(scan_type, workspace, target):
        assert scan_type != "trivy", "없는 이미지를 Trivy 에 넘겼다 — 이게 사고였다"
        return {"status": "ok", "critical_count": 0, "high_count": 0}

    monkeypatch.setattr(deploy, "_execute_scan", _must_not_scan_trivy)

    gate = asyncio.run(deploy._run_pre_deploy_security_gate(_plan_request()))

    assert gate["unverified"] is True
    assert gate["reports"]["trivy"]["status"] == "unverified"
    #: 화면까지 사유가 올라가야 사용자가 미검증임을 안다.
    assert any("미빌드" in r or "미검증" in r for r in gate["risk_reasons"])
    #: 확인된 위험은 아니므로 차단(blocker)은 아니다.
    assert gate["blockers"] == []


def test_스캔_error_는_더이상_조용히_통과되지_않는다(monkeypatch) -> None:
    monkeypatch.setattr(deploy, "_local_image_exists", lambda image: True)

    async def _broken_scan(scan_type, workspace, target):
        return {
            "status": "error", "scan_type": scan_type, "critical_count": 0,
            "high_count": 0, "findings": [],
            "summary": "Docker is not available on this host.",
        }

    monkeypatch.setattr(deploy, "_execute_scan", _broken_scan)

    gate = asyncio.run(deploy._run_pre_deploy_security_gate(_plan_request()))

    assert gate["unverified"] is True
    assert any("스캔 실패" in r for r in gate["risk_reasons"])


# ---------------------------------------------------------------------------
# 2. 플랜 — 승인 강도 반영
# ---------------------------------------------------------------------------


def test_unverified_플랜은_이중확인으로_올라가고_실행시_스캔_대기열에_남는다(monkeypatch) -> None:
    async def _unverified_gate(request):
        return {"blockers": [], "risk_reasons": ["Trivy: 미빌드 — 미검증"],
                "elevated": False, "unverified": True, "reports": {}}

    fake = _fake_plan()

    class _Agent:
        async def create_plan(self, request):
            return fake

    monkeypatch.setattr(deploy, "_run_pre_deploy_security_gate", _unverified_gate)
    monkeypatch.setattr(deploy, "_get_deploy_agent", lambda: _Agent())
    monkeypatch.setattr(deploy, "_refresh_rollback_target", lambda plan: (None, ""))
    deploy._plans_pending_image_scan.clear()

    plan = asyncio.run(deploy.create_deployment_plan(_plan_request()))

    assert plan.approval_level == ApprovalLevel.DOUBLE_CONFIRM
    #: 위험이 '확인'된 것은 아니므로 HIGH 로 끌어올리지는 않는다.
    assert plan.risk_level == RiskLevel.LOW
    assert deploy._plans_pending_image_scan.get(plan.plan_id) == plan.image


# ---------------------------------------------------------------------------
# 3. 실행 — 빌드 후 1회 스캔과 차단
# ---------------------------------------------------------------------------


def test_실행시_빌드후_스캔이_돌고_CRITICAL_이면_컨테이너를_건드리기_전에_차단(monkeypatch) -> None:
    plan = _fake_plan()
    deploy._deployment_plans[plan.plan_id] = plan
    deploy._plans_pending_image_scan[plan.plan_id] = plan.image

    monkeypatch.setattr(deploy, "_local_image_exists", lambda image: True)

    scanned: list[str] = []
    async def image_id(image):
        return 'sha256:' + 'a' * 64
    monkeypatch.setattr(deploy, '_local_image_id', image_id)

    async def _critical_scan(scan_type, workspace, target):
        scanned.append(target)
        return {"status": "ok", "scan_type": "trivy", "critical_count": 2,
                "high_count": 0, "findings": [], "summary": "2 critical"}

    monkeypatch.setattr(deploy, "_execute_scan", _critical_scan)

    def _docker_must_not_run(*args, **kwargs):
        raise AssertionError("차단됐어야 하는데 docker 명령이 실행됐다")

    monkeypatch.setattr(deploy.subprocess, "run", _docker_must_not_run)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(deploy.execute_deployment(
            deploy.ExecuteRequest(plan_id=plan.plan_id, approved=True)
        ))

    assert exc.value.status_code == 400
    assert "CRITICAL" in str(exc.value.detail)
    #: 스캔이 실제로 1회 돌았다 — 이게 DoD 다.
    assert scanned == ['sha256:' + 'a' * 64]
    # A rejected request must still require scanning on retry.
    assert plan.plan_id in deploy._plans_pending_image_scan
    with pytest.raises(HTTPException):
        asyncio.run(deploy.execute_deployment(deploy.ExecuteRequest(plan_id=plan.plan_id, approved=True)))
    assert len(scanned) == 2

    deploy._deployment_plans.pop(plan.plan_id, None)
    deploy._plans_pending_image_scan.pop(plan.plan_id, None)


def test_실행_취소시에도_대기열이_정리된다(monkeypatch) -> None:
    plan = _fake_plan()
    deploy._deployment_plans[plan.plan_id] = plan
    deploy._plans_pending_image_scan[plan.plan_id] = plan.image

    result = asyncio.run(deploy.execute_deployment(
        deploy.ExecuteRequest(plan_id=plan.plan_id, approved=False)
    ))

    assert result["status"] == "cancelled"
    assert plan.plan_id not in deploy._plans_pending_image_scan


# ---------------------------------------------------------------------------
# 4. 음성 대조 — 정상 스캔 경로는 그대로
# ---------------------------------------------------------------------------


def test_clean_preview_still_requires_scanning_the_image_built_at_execution(monkeypatch) -> None:
    monkeypatch.setattr(deploy, "_local_image_exists", lambda image: True)

    async def _clean_scan(scan_type, workspace, target):
        return {"status": "ok", "scan_type": scan_type, "critical_count": 0,
                "high_count": 0, "findings": [], "summary": "clean"}

    fake = _fake_plan()

    class _Agent:
        async def create_plan(self, request):
            return fake

    monkeypatch.setattr(deploy, "_execute_scan", _clean_scan)
    monkeypatch.setattr(deploy, "_get_deploy_agent", lambda: _Agent())
    monkeypatch.setattr(deploy, "_refresh_rollback_target", lambda plan: (None, ""))
    deploy._plans_pending_image_scan.clear()

    plan = asyncio.run(deploy.create_deployment_plan(_plan_request()))

    assert plan.approval_level == ApprovalLevel.CONFIRM
    assert deploy._plans_pending_image_scan == {plan.plan_id: plan.image}
