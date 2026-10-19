"""Execution permission must precede effects, including cached repairs and approval."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from execution_policy import ExecutionDeniedError, ExecutionPolicy
from grounded_repair.knowledge import KnowledgeIndex, Passage
from grounded_repair.pipeline import RepairPipeline
from grounded_repair.store import RepairStore
from llm.base import LLMResponse
from opa_gate import OPADecision, OPAGate, OPAResult


class Gate:
    _url = "http://policy.invalid"

    def __init__(self, blocked=()):
        self.blocked, self.calls = set(blocked), []

    def evaluate(self, path, facts, approval_level):
        assert path == "recoder/execution/allow" and approval_level == 3
        self.calls.append(facts)
        denied = facts["action"] in self.blocked
        return OPAResult(
            OPADecision.DENY if denied else OPADecision.ALLOW,
            reason="operator policy",
            policy_version="test-policy-v1",
        )


class Router:
    def __init__(self):
        self.calls = 0

    async def call_repair(self, *args, **kwargs):
        self.calls += 1
        data = {
            "explanation": "Use a named route",
            "guidance": [],
            "evidence_ids": ["doc"],
            "edits": [{"path": "app.js", "content": "fixed", "evidence_ids": ["doc"]}],
        }
        return LLMResponse(
            text=json.dumps(data),
            parsed=data,
            metadata={"llm_call_record": {"model": "fixture", "estimated_cost_usd": 0}},
        )


class Verifier:
    identity = "fixture-verifier"

    def __init__(self):
        self.calls = []

    def __call__(self, root):
        content = (root / "app.js").read_text(encoding="utf-8")
        self.calls.append(content)
        return {"passed": content == "fixed", "output": "Missing parameter name"}


def setup(tmp_path, policy):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.js").write_text("broken", encoding="utf-8")
    store = RepairStore(tmp_path / "runs.db", policy)
    index = KnowledgeIndex(
        [
            Passage(
                "doc",
                "Express",
                "https://expressjs.com/en/guide/migrating-5/",
                "Missing parameter name wildcard route",
                "rev",
                "2026-10-20",
            )
        ]
    )
    router, verifier = Router(), Verifier()
    pipeline = RepairPipeline(
        router, index, store, verifier, lambda *a, **k: SimpleNamespace(errors=[])
    )
    return root, store, router, verifier, pipeline


def run(pipeline, root):
    return asyncio.run(pipeline.run(str(root), "Missing parameter name wildcard route"))


@pytest.mark.parametrize(
    "action,models,verifications",
    [
        ("repair.inspect", 0, 0),
        ("repair.verify", 0, 0),
        ("repair.generate", 0, 1),
        ("repair.edit", 1, 1),
    ],
)
def test_denial_precedes_effects(tmp_path, monkeypatch, action, models, verifications):
    gate = Gate([action])
    root, _, router, verifier, pipeline = setup(tmp_path, ExecutionPolicy("opa", gate))
    if action == "repair.edit":

        def unexpected_write(*args):
            pytest.fail("Candidate writing must not precede policy approval")

        monkeypatch.setattr("grounded_repair.pipeline.safe_path", unexpected_write)
    result = run(pipeline, root)
    assert result["status"] == "policy_denied" and not result["approval_required"]
    assert router.calls == models and len(verifier.calls) == verifications
    assert result["policy_checks"][-1]["allowed"] is False
    assert (root / "app.js").read_text(encoding="utf-8") == "broken"
    assert all("workspace" not in call and "content" not in call for call in gate.calls)


def test_candidate_verification_is_a_new_permission_check(tmp_path):
    class RevokeAfterBaseline(Gate):
        def evaluate(self, path, facts, approval_level):
            if facts["action"] == "repair.verify" and any(
                c["action"] == "repair.verify" for c in self.calls
            ):
                self.blocked.add("repair.verify")
            return super().evaluate(path, facts, approval_level)

    root, _, router, verifier, pipeline = setup(
        tmp_path, ExecutionPolicy("opa", RevokeAfterBaseline())
    )
    result = run(pipeline, root)
    assert result["status"] == "policy_denied"
    assert router.calls == 1 and verifier.calls == ["broken"]


def test_rule_fix_is_authorized_before_modifying_snapshot(tmp_path):
    root, _, router, verifier, pipeline = setup(
        tmp_path, ExecutionPolicy("opa", Gate(["repair.rule_fix"]))
    )
    issue = SimpleNamespace(
        code="FIXTURE_RULE", auto_fix=True, to_dict=lambda: {"code": "FIXTURE_RULE"}
    )
    pipeline.readiness = lambda *a, **k: SimpleNamespace(errors=[issue])
    pipeline.rule_fix = lambda *a: pytest.fail("Denied rule must not execute")
    assert run(pipeline, root)["status"] == "policy_denied"
    assert router.calls == 0 and verifier.calls == []


def test_revocation_between_prepare_and_apply_preserves_original_and_audit(tmp_path):
    gate = Gate()
    root, store, _, _, pipeline = setup(tmp_path, ExecutionPolicy("opa", gate))
    prepared = run(pipeline, root)
    assert prepared["status"] == "ready_for_approval"
    with store.connect() as db:
        created = db.execute("SELECT created FROM repair_runs").fetchone()[0]
    gate.blocked.add("repair.apply")
    with pytest.raises(ExecutionDeniedError):
        store.approve(prepared["id"], str(root))
    assert (root / "app.js").read_text(encoding="utf-8") == "broken"
    assert store.get(prepared["id"])["policy_checks"][-1]["allowed"] is False
    with store.connect() as db:
        assert db.execute("SELECT created FROM repair_runs").fetchone()[0] == created
    gate.blocked.clear()
    assert store.approve(prepared["id"], str(root))["status"] == "applied"
    assert (root / "app.js").read_text(encoding="utf-8") == "fixed"


def test_cached_suggestion_still_needs_current_edit_permission(tmp_path):
    gate = Gate()
    root, _, router, verifier, pipeline = setup(tmp_path, ExecutionPolicy("opa", gate))
    assert run(pipeline, root)["status"] == "ready_for_approval"
    gate.blocked.add("repair.edit")
    result = run(pipeline, root)
    assert result["status"] == "policy_denied" and result["cache_hit"]
    assert router.calls == 1 and verifier.calls == ["broken", "fixed", "broken"]


def test_policy_mode_change_invalidates_repair_cache(tmp_path):
    root, store, router, _, pipeline = setup(tmp_path, ExecutionPolicy("local"))
    assert run(pipeline, root)["status"] == "ready_for_approval"
    policy = ExecutionPolicy("opa", Gate(["repair.generate"]))
    pipeline.policy = store.policy = policy
    result = run(pipeline, root)
    assert result["status"] == "policy_denied" and not result["cache_hit"]
    assert router.calls == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        True,
        {"result": True},
        {"result": {"decision": "unknown"}},
        {"result": {"decision": "allow"}},
        {"result": {"decision": "allow_with_approval", "policy_bundle_version": "v1"}},
    ],
    ids=[
        "offline",
        "missing",
        "boolean",
        "boolean-result",
        "unknown",
        "missing-version",
        "needs-approval",
    ],
)
def test_unavailable_or_malformed_policy_never_grants_permission(tmp_path, monkeypatch, response):
    gate = OPAGate()
    monkeypatch.setattr(gate, "_post", lambda *a: response)
    with pytest.raises(ExecutionDeniedError):
        ExecutionPolicy("opa", gate).authorize("repair.verify", tmp_path, snapshot=True)


@pytest.mark.parametrize("name", ["../outside.txt", ".env", "tests/app.test.js", "missing.js"])
def test_remote_allow_cannot_override_local_file_restrictions(tmp_path, name):
    gate = Gate()
    with pytest.raises(ExecutionDeniedError):
        ExecutionPolicy("opa", gate).authorize(
            "repair.apply", tmp_path, paths=[name], verified=True, approved=True
        )
    assert gate.calls == []


@pytest.mark.parametrize("verified,approved", [(False, True), (True, False), (True, "true")])
def test_approval_flags_must_be_trusted_booleans(tmp_path, verified, approved):
    (tmp_path / "app.js").write_text("original", encoding="utf-8")
    with pytest.raises(ExecutionDeniedError):
        ExecutionPolicy("local").authorize(
            "repair.apply", tmp_path, paths=["app.js"], verified=verified, approved=approved
        )


def test_invalid_mode_is_not_an_offline_fallback(monkeypatch):
    monkeypatch.setenv("RECODER_EXECUTION_POLICY", "opaa")
    with pytest.raises(ValueError, match="local or opa"):
        ExecutionPolicy()


def test_unknown_action_or_original_verification_cannot_reach_opa(tmp_path):
    gate = Gate()
    policy = ExecutionPolicy("opa", gate)
    with pytest.raises(ExecutionDeniedError):
        policy.authorize("shell.execute", tmp_path, snapshot=True)
    with pytest.raises(ExecutionDeniedError):
        policy.authorize("repair.verify", tmp_path, snapshot=False)
    assert gate.calls == []


def test_policy_audit_tampering_invalidates_approval(tmp_path):
    root, store, _, _, pipeline = setup(tmp_path, ExecutionPolicy("local"))
    result = run(pipeline, root)
    stored = store.get(result["id"])
    stored["policy_checks"][0]["reason"] = "forged audit"
    with store.connect() as db:
        db.execute("UPDATE repair_runs SET payload=?", (json.dumps(stored),))
    with pytest.raises(ValueError, match="modified after verification"):
        store.approve(result["id"], str(root))


def test_mcp_denial_precedes_handler_and_keeps_audit(tmp_path, monkeypatch):
    import execution_policy
    import mcp_server

    policy = ExecutionPolicy("opa", Gate(["mcp.call"]))
    monkeypatch.setattr(execution_policy, "ExecutionPolicy", lambda: policy)
    monkeypatch.setenv("RECODER_HOME", str(tmp_path / "audit"))

    async def forbidden(arguments):
        pytest.fail("Denied MCP handler must not execute")

    monkeypatch.setitem(mcp_server._TOOL_DISPATCH, "recoder_analyze", forbidden)
    response = asyncio.run(
        mcp_server._handle_message(
            {
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "recoder_analyze",
                    "arguments": {
                        "workspace_path": str(tmp_path),
                        "error_message": "private-source",
                    },
                },
            }
        )
    )
    assert response["result"]["isError"] is True
    audit = (tmp_path / "audit/mcp_calls.jsonl").read_text(encoding="utf-8")
    assert not json.loads(audit)["ok"] and "private-source" not in audit
    assert json.loads(audit)["execution_policy"]["decision"] == "deny"


def test_mcp_allowed_handler_still_runs_and_is_audited(tmp_path, monkeypatch):
    import execution_policy
    import mcp_server

    policy = ExecutionPolicy("opa", Gate())
    monkeypatch.setattr(execution_policy, "ExecutionPolicy", lambda: policy)
    monkeypatch.setenv("RECODER_HOME", str(tmp_path / "audit"))
    calls = []

    async def handler(arguments):
        calls.append(arguments)
        return {"content": [{"type": "text", "text": "fixture result"}], "isError": False}

    monkeypatch.setitem(mcp_server._TOOL_DISPATCH, "recoder_analyze", handler)
    response = asyncio.run(
        mcp_server._handle_message(
            {
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "recoder_analyze",
                    "arguments": {"workspace_path": str(tmp_path), "error_message": "fixture"},
                },
            }
        )
    )
    assert len(calls) == 1 and not response["result"]["isError"]
    assert response["result"]["_meta"]["execution_policy"]["allowed"]
