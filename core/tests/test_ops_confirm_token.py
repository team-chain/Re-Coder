"""
ops 2단계 확인 — Codex P1: 서버 발급 confirm_token 강제 + P2 실패 복원.
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


def _dc_proposal(pid="p", requested_by="alice", confirm_token="SERVER_ISSUED"):
    p = ResponseProposal(
        proposal_id=pid, alert_id="a", action_type=ActionType.DOCKER_RESTART,
        target_container="app", risk_level=RiskLevel.HIGH, risk_reasons=["x"],
        approval_level=ApprovalLevel.DOUBLE_CONFIRM,
        requested_by=requested_by, confirm_token=confirm_token)
    return p


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ops._proposals.clear()
    async def _fake_ssh(**kw):
        return {"exit_code": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(ops, "_ssh_exec", _fake_ssh)
    yield
    ops._proposals.clear()


def _approve(**kw):
    return asyncio.run(ops.approve_response(ops.ApproveResponseRequest(**kw)))


def test_arbitrary_confirm_token_is_rejected():
    """[Codex P1 회귀] 서버가 발급하지 않은 임의 토큰은 실행되지 않는다."""
    ops._proposals["p"] = _dc_proposal()
    with pytest.raises(HTTPException) as exc:
        _approve(proposal_id="p", approved=True, ssh_host="h", ssh_key_path="/k",
                 confirm_token="아무거나", second_approver="bob")
    assert exc.value.status_code == 403
    assert "p" in ops._proposals   # 실패 후 복원

def test_second_approver_must_differ_from_requester():
    """[Codex P1 회귀] 두 번째 승인자가 최초 요청자와 같으면 거부."""
    ops._proposals["p"] = _dc_proposal(requested_by="alice", confirm_token="TOK")
    with pytest.raises(HTTPException) as exc:
        _approve(proposal_id="p", approved=True, ssh_host="h", ssh_key_path="/k",
                 confirm_token="TOK", second_approver="alice")
    assert exc.value.status_code == 422
    assert "p" in ops._proposals


def test_valid_server_token_and_distinct_approver_executes():
    """[음성 대조] 서버 발급 토큰 + 다른 승인자면 정상 실행."""
    ops._proposals["p"] = _dc_proposal(requested_by="alice", confirm_token="TOK")
    out = _approve(proposal_id="p", approved=True, ssh_host="h", ssh_key_path="/k",
                   confirm_token="TOK", second_approver="bob")
    assert out["status"] == "executed"
    assert "p" not in ops._proposals   # 실행 후 소비


def test_missing_ssh_restores_proposal():
    """[Codex P2 회귀] 실행 전 실패(SSH 파라미터 누락)면 proposal 이 복원된다."""
    ops._proposals["p"] = _dc_proposal(confirm_token="TOK")
    with pytest.raises(HTTPException) as exc:
        _approve(proposal_id="p", approved=True,
                 confirm_token="TOK", second_approver="bob")  # ssh 없음
    assert exc.value.status_code == 422
    assert "p" in ops._proposals   # 재시도 가능
    # 재시도(이번엔 ssh 제공)는 성공해야 한다.
    out = _approve(proposal_id="p", approved=True, ssh_host="h", ssh_key_path="/k",
                   confirm_token="TOK", second_approver="bob")
    assert out["status"] == "executed"


def test_analyze_issues_confirm_token_for_double_confirm():
    """분석 단계가 DOUBLE_CONFIRM proposal 에 서버 토큰을 발급한다."""
    from schemas import AlertRecord, AlertType
    alert = AlertRecord(
        alert_id="al1", source="monitor",
        alert_type=AlertType.CRASH,
        severity="high",
        container_name="app")
    import api.routes.ops as _o
    _o._get_ops_agent = lambda: None  # placeholder 경로 강제
    ops._alerts["al1"] = alert
    resp = asyncio.run(ops.analyze_incident(
        ops.AnalyzeIncidentRequest(alert_id="al1", requested_by="alice")))
    import json as _json
    body = _json.loads(bytes(resp.body).decode()) if hasattr(resp, "body") else resp
    # JSONResponse 면 confirm_token 이 응답에 포함
    assert "confirm_token" in body and body["confirm_token"]
    # 저장된 proposal 에도 심겨 있고 requested_by 고정
    pid = body["proposal_id"]
    stored = ops._proposals[pid]
    assert stored.confirm_token == body["confirm_token"]
    assert stored.requested_by == "alice"
