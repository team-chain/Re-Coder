"""ECS 사전 점검 실패 사유 — 항목 이름만 남기지 않는다 (실기기 검증 C4).

실기기에서 ECS 배포가 "Preflight 점검 실패 — 배포를 중단합니다 / 실패 항목: IAM Role
'ecsTaskExecutionRole' 존재 확인 / 사이드바의 점검 결과에서 실패한 항목을 확인하세요."
로 끝났다. 무엇이 없는지(역할이 없다)·어떻게 만들지는 각 점검이 이미 detail·fix_guide
로 들고 있었는데 파이프라인이 이름만 옮겼고, 조치 문구는 존재하지 않는 화면을 가리켰다.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agents import ecs_agent as ecs_agent_mod


def _agent_with_preflight(checks):
    class _Preflight:
        async def run(self, **_kwargs):
            return SimpleNamespace(passed=all(c.passed for c in checks), checks=checks)

    agent = ecs_agent_mod.ECSAgent.__new__(ecs_agent_mod.ECSAgent)
    agent._preflight = _Preflight()
    return agent


def _req():
    return SimpleNamespace(
        cluster="c", service="s", region="ap-northeast-2", task_definition_family="f",
        provision=True, ecr_repo="", image="app:latest",
    )


def test_실패_항목에_상세와_조치가_붙는다():
    checks = [
        SimpleNamespace(
            name="IAM Role 'ecsTaskExecutionRole' 존재 확인", passed=False, severity="error",
            detail="IAM Role 'ecsTaskExecutionRole'이 없습니다",
            fix_guide="IAM 콘솔에서 역할을 만드세요. CLI: aws iam create-role …",
        ),
        SimpleNamespace(name="리전", passed=True, severity="error", detail="ok", fix_guide=None),
    ]
    agent = _agent_with_preflight(checks)
    rec = SimpleNamespace(preflight_passed=None, error_detail=None, error_remedy=None)

    rec = asyncio.run(agent._step_preflight(_req(), rec))

    assert rec.preflight_passed is False
    assert "IAM Role 'ecsTaskExecutionRole'이 없습니다" in rec.error_detail
    assert "aws iam create-role" in rec.error_detail, "조치(fix_guide)가 사유에 없다"
    assert "사이드바" not in (rec.error_remedy or ""), "존재하지 않는 화면을 가리킨다"


def test_경고_수준_실패는_실패_항목에_넣지_않는다():
    checks = [SimpleNamespace(name="선택 항목", passed=False, severity="warning", detail="x", fix_guide=None)]
    agent = _agent_with_preflight(checks)
    rec = SimpleNamespace(preflight_passed=None, error_detail=None, error_remedy=None)

    rec = asyncio.run(agent._step_preflight(_req(), rec))

    assert rec.error_detail is None


def test_IAM_역할_점검_안내는_역할_이름과_CLI_를_담는다(monkeypatch):
    from agents import preflight_agent as pf

    class _IAM:
        def get_role(self, RoleName):
            raise Exception("An error occurred (NoSuchEntity) when calling the GetRole operation")

    agent = pf.PreflightAgent.__new__(pf.PreflightAgent)
    monkeypatch.setattr(agent, "_iam_client", lambda: _IAM(), raising=False)

    check = asyncio.run(agent._check_iam_role("ecsTaskExecutionRole", "ap-northeast-2"))

    assert check.passed is False
    assert "ecsTaskExecutionRole" in check.fix_guide
    assert "AmazonECSTaskExecutionRolePolicy" in check.fix_guide
    assert "aws iam create-role --role-name ecsTaskExecutionRole" in check.fix_guide
