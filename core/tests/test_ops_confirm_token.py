"""
ops 2단계 확인 — 인증된 2인 승인 + 실행 전/후 실패 상태 회귀.
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


def _approve(authenticated_actor="bob", **kw):
    return asyncio.run(ops.approve_response(
        ops.ApproveResponseRequest(**kw),
        authenticated_actor=authenticated_actor,
    ))


def test_arbitrary_confirm_token_is_rejected():
    """[Codex P1 회귀] 서버가 발급하지 않은 임의 토큰은 실행되지 않는다."""
    ops._proposals["p"] = _dc_proposal()
    with pytest.raises(HTTPException) as exc:
        _approve(proposal_id="p", approved=True, ssh_host="h", ssh_key_path="/k",
                 confirm_token="아무거나")
    assert exc.value.status_code == 403
    assert "p" in ops._proposals   # 실패 후 복원

def test_same_authenticated_actor_cannot_spoof_second_approver():
    """[Codex P1 회귀] 본문 이름이 달라도 같은 인증 주체면 거부."""
    ops._proposals["p"] = _dc_proposal(requested_by="alice", confirm_token="TOK")
    with pytest.raises(HTTPException) as exc:
        _approve(authenticated_actor="alice", proposal_id="p", approved=True,
                 ssh_host="h", ssh_key_path="/k", confirm_token="TOK",
                 second_approver="bob")
    assert exc.value.status_code == 422
    assert "p" in ops._proposals


def test_valid_server_token_and_distinct_approver_executes():
    """[음성 대조] 서버 발급 토큰 + 다른 승인자면 정상 실행."""
    ops._proposals["p"] = _dc_proposal(requested_by="alice", confirm_token="TOK")
    out = _approve(proposal_id="p", approved=True, ssh_host="h", ssh_key_path="/k",
                   confirm_token="TOK")
    assert out["status"] == "executed"
    assert "p" not in ops._proposals   # 실행 후 소비


def test_missing_ssh_restores_proposal():
    """[Codex P2 회귀] 실행 전 실패(SSH 파라미터 누락)면 proposal 이 복원된다."""
    ops._proposals["p"] = _dc_proposal(confirm_token="TOK")
    with pytest.raises(HTTPException) as exc:
        _approve(proposal_id="p", approved=True,
                 confirm_token="TOK")  # ssh 없음
    assert exc.value.status_code == 422
    assert "p" in ops._proposals   # 재시도 가능
    # 재시도(이번엔 ssh 제공)는 성공해야 한다.
    out = _approve(proposal_id="p", approved=True, ssh_host="h", ssh_key_path="/k",
                   confirm_token="TOK")
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
        ops.AnalyzeIncidentRequest(alert_id="al1", requested_by="mallory"),
        authenticated_actor="alice",
    ))
    import json as _json
    body = _json.loads(bytes(resp.body).decode()) if hasattr(resp, "body") else resp
    # JSONResponse 면 confirm_token 이 응답에 포함
    assert "confirm_token" in body and body["confirm_token"]
    # 저장된 proposal 에도 심겨 있고 requested_by 고정
    pid = body["proposal_id"]
    stored = ops._proposals[pid]
    assert stored.confirm_token == body["confirm_token"]
    assert stored.requested_by == "alice"


def test_actor_identity_is_derived_from_configured_token(monkeypatch):
    monkeypatch.setenv(
        "RECODER_OPS_APPROVER_TOKENS",
        '{"alice":"secret-a","bob":"secret-b"}',
    )
    assert ops._authenticated_ops_actor("secret-b") == "bob"
    with pytest.raises(HTTPException) as exc:
        ops._authenticated_ops_actor("not-configured")
    assert exc.value.status_code == 401


def test_ambiguous_ssh_failure_consumes_proposal(monkeypatch):
    """SSH dispatch 후 오류는 재실행 가능 상태로 되돌리지 않는다."""
    async def _ambiguous_failure(**kw):
        raise TimeoutError("connection lost after dispatch")

    monkeypatch.setattr(ops, "_ssh_exec", _ambiguous_failure)
    ops._proposals["p"] = _dc_proposal(requested_by="alice", confirm_token="TOK")
    with pytest.raises(HTTPException) as exc:
        _approve(proposal_id="p", approved=True, ssh_host="h", ssh_key_path="/k",
                 confirm_token="TOK")
    assert exc.value.status_code == 502
    assert "p" not in ops._proposals
    assert "소비" in exc.value.detail


def test_definite_pre_dispatch_ssh_failure_restores_proposal(monkeypatch):
    """로컬 SSH 시작 실패는 원격 실행이 없으므로 안전하게 재시도 가능하다."""
    async def _not_dispatched(**kw):
        raise ops._SSHNotDispatchedError("SSH client not found")

    monkeypatch.setattr(ops, "_ssh_exec", _not_dispatched)
    ops._proposals["p"] = _dc_proposal(requested_by="alice", confirm_token="TOK")
    with pytest.raises(HTTPException) as exc:
        _approve(proposal_id="p", approved=True, ssh_host="h", ssh_key_path="/k",
                 confirm_token="TOK")
    assert exc.value.status_code == 503
    assert "p" in ops._proposals


def test_double_confirm_fails_closed_without_authenticated_requester():
    proposal = _dc_proposal(requested_by=None, confirm_token="TOK")
    ops._proposals["p"] = proposal
    with pytest.raises(HTTPException) as exc:
        _approve(proposal_id="p", approved=True, ssh_host="h", ssh_key_path="/k",
                 confirm_token="TOK")
    assert exc.value.status_code == 409
    assert "p" in ops._proposals
