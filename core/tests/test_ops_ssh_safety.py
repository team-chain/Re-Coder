"""ops SSH — 옵션 주입 차단 · 호스트 키 확인 · fetch-incidents 승인자 인증 (3.0.2)."""
import asyncio
import json
import subprocess

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.routes import ops
from schemas import ActionType, ApprovalLevel, ResponseProposal, RiskLevel


@pytest.fixture(autouse=True)
def _clean():
    ops._proposals.clear()
    yield
    ops._proposals.clear()


@pytest.fixture
def ssh_calls(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ops.subprocess, "run", fake_run)
    return calls


@pytest.mark.parametrize("host", ["-oProxyCommand=touch /tmp/pwn", "-x", "host name", "h;id", "a/b", "", "..", "h..x"])
def test_bad_ssh_host_is_rejected_before_ssh_runs(host, ssh_calls):
    with pytest.raises(HTTPException) as err:
        asyncio.run(ops._ssh_exec(host=host, user="ec2-user", key_path="/k", command="uptime"))
    assert err.value.status_code == 422
    assert ssh_calls == []


@pytest.mark.parametrize("user", ["-oProxyCommand=x", "root@evil", "a b", "", "-l"])
def test_bad_ssh_user_is_rejected_before_ssh_runs(user, ssh_calls):
    with pytest.raises(HTTPException) as err:
        asyncio.run(ops._ssh_fetch_file(host="10.0.0.5", user=user, key_path="/k", port=22, remote_path="/x"))
    assert err.value.status_code == 422
    assert ssh_calls == []


@pytest.mark.parametrize("host", ["10.0.0.5", "ec2-1-2-3-4.compute.amazonaws.com", "fe80::1", "my_host"])
def test_valid_targets_use_double_dash_and_accept_new(host, ssh_calls):
    asyncio.run(ops._ssh_exec(host=host, user="ec2-user", key_path="/k", command="uptime"))
    cmd = ssh_calls[0]
    assert "StrictHostKeyChecking=no" not in cmd
    assert "StrictHostKeyChecking=accept-new" in cmd
    dash = cmd.index("--")
    assert cmd[dash + 1] == f"ec2-user@{host}"
    assert cmd[-1] == "uptime"


def test_fetch_file_failure_hides_stderr(monkeypatch):
    monkeypatch.setattr(ops.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 255, stdout="", stderr="secret-path /home/me/.ssh/id_rsa denied"))
    with pytest.raises(HTTPException) as err:
        asyncio.run(ops._ssh_fetch_file(host="h1", user="u", key_path="/k", port=22, remote_path="/x"))
    assert err.value.status_code == 502
    assert "id_rsa" not in err.value.detail and "오류 ID" in err.value.detail


def test_approve_with_injected_host_restores_proposal(ssh_calls):
    ops._proposals["p1"] = ResponseProposal(
        alert_id="a1", action_type=ActionType.DOCKER_RESTART, target_container="app",
        risk_level=RiskLevel.LOW, risk_reasons=["t"], approval_level=ApprovalLevel.CONFIRM, requested_by="alice")
    req = ops.ApproveResponseRequest(proposal_id="p1", approved=True, ssh_host="-oProxyCommand=id", ssh_key_path="/k")
    with pytest.raises(HTTPException) as err:
        asyncio.run(ops.approve_response(req, authenticated_actor="bob"))
    assert err.value.status_code == 422
    assert "p1" in ops._proposals, "검증 실패는 실행 전 실패 — 제안을 되돌려야 한다"
    assert ssh_calls == []


def test_fetch_incidents_requires_ops_approver(monkeypatch, ssh_calls):
    app = FastAPI()
    app.include_router(ops.router)
    client = TestClient(app)
    body = {"host": "10.0.0.5", "ssh_key_path": "/k"}
    monkeypatch.setenv("RECODER_OPS_APPROVER_TOKENS", json.dumps({"alice": "tok-a", "bob": "tok-b"}))
    assert client.post("/api/ops/fetch-incidents", json=body).status_code == 401
    assert client.post("/api/ops/fetch-incidents", json=body, headers={"X-Ops-Actor-Token": "nope"}).status_code == 401
    assert ssh_calls == []
    ok = client.post("/api/ops/fetch-incidents", json=body, headers={"X-Ops-Actor-Token": "tok-a"})
    assert ok.status_code == 200 and ok.json() == []
    assert len(ssh_calls) == 1
    monkeypatch.delenv("RECODER_OPS_APPROVER_TOKENS")
    assert client.post("/api/ops/fetch-incidents", json=body, headers={"X-Ops-Actor-Token": "tok-a"}).status_code == 503
