"""
ops approve — Codex P1: 승인 강도 서버 검증 + 원자적 소비 회귀.
"""
import asyncio
import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from fastapi import HTTPException           # noqa: E402
from api.routes import ops                  # noqa: E402
from schemas import ApprovalLevel, ResponseProposal, ActionType, RiskLevel  # noqa: E402


def _proposal(level, pid="p1"):
    p = ResponseProposal(
        alert_id="a1",
        action_type=ActionType.DOCKER_RESTART,
        target_container="app",
        risk_level=RiskLevel.HIGH,
        risk_reasons=["t"],
        approval_level=level,
        requested_by="alice",
    )
    return p  # 저장은 _proposals[dict-key] 로 직접 — 모델은 proposal_id 필드 없음


def _approve(**kw):
    req = ops.ApproveResponseRequest(**kw)
    return asyncio.run(ops.approve_response(req, authenticated_actor="bob"))


@pytest.fixture(autouse=True)
def _clean():
    ops._proposals.clear()
    yield
    ops._proposals.clear()


def test_double_confirm_requires_second_approver(tmp_path):
    """[Codex P1 회귀] DOUBLE_CONFIRM 은 approved=true 하나로 실행되면 안 된다."""
    ops._proposals["p1"] = _proposal(ApprovalLevel.DOUBLE_CONFIRM)
    with pytest.raises(HTTPException) as exc:
        _approve(proposal_id="p1", approved=True,
                 ssh_host="h", ssh_key_path="/k")
    assert exc.value.status_code == 428
    # 미완료 승인은 되돌려져 재시도 가능해야 한다.
    assert "p1" in ops._proposals


def test_blocked_level_refuses_execution():
    """[Codex P1 회귀] BLOCKED 는 실행 자체가 거부된다."""
    ops._proposals["p2"] = _proposal(ApprovalLevel.BLOCKED, "p2")
    with pytest.raises(HTTPException) as exc:
        _approve(proposal_id="p2", approved=True, ssh_host="h", ssh_key_path="/k")
    assert exc.value.status_code == 403


def test_double_approve_consumes_atomically():
    """[Codex P1 회귀] 같은 proposal_id 로 두 번 승인하면 두 번째는 404 —
    pop 으로 원자 소비하므로 원격 명령이 두 번 실행되지 않는다."""
    ops._proposals["p3"] = _proposal(ApprovalLevel.CONFIRM, "p3")
    # 거부(=소비)를 두 번: 첫 번째만 성공, 두 번째는 404
    out1 = _approve(proposal_id="p3", approved=False)
    assert out1["status"] == "rejected"
    with pytest.raises(HTTPException) as exc:
        _approve(proposal_id="p3", approved=False)
    assert exc.value.status_code == 404
