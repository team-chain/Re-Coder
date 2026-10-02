from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grounded_repair.knowledge import KnowledgeIndex, Passage
from grounded_repair.logs import summarize
from grounded_repair.pipeline import RepairPipeline
from grounded_repair.store import RepairStore
from grounded_repair.workspace import DockerVerifier, manifest, safe_path
from llm.base import LLMResponse


def passage(id="express", text="Missing parameter name Express 5 wildcard named route"):
    return Passage(id, "Express routes", "https://expressjs.com/en/guide/migrating-5/", text, "5", "2026-10-03")


def suggestion(content="fixed", citations=None):
    refs = citations if citations is not None else ["express"]
    return {"explanation": "Use a named route", "guidance": [], "evidence_ids": refs,
            "edits": [{"path": "app.js", "content": content, "evidence_ids": refs}]}


class Router:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def call_repair(self, request, *, tier, run_id):
        self.calls.append((tier, request))
        data = next(self.responses)
        return LLMResponse(text=json.dumps(data), parsed=data, metadata={"llm_call_record": {
            "model": tier, "estimated_cost_usd": .01, "token_source": "api", "input_tokens": 20,
            "output_tokens": 10, "fallback_used": False}})


class Verifier:
    identity = "test-rebuild-v1"

    def __init__(self):
        self.contents = []

    def __call__(self, root):
        text = (root / "app.js").read_text()
        self.contents.append(text)
        return {"passed": text == "fixed", "output": "Missing parameter name Express 5 wildcard"}


def no_rules(*args, **kwargs):
    return SimpleNamespace(errors=[])


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.js").write_text("broken")
    store = RepairStore(tmp_path / "runs.db")
    index = KnowledgeIndex([passage()])
    return root, store, index


def run(p, root, **kwargs):
    return asyncio.run(p.run(str(root), "Missing parameter name Express 5 wildcard", **kwargs))


def test_small_model_success_requires_rebuild_then_explicit_approval(setup):
    root, store, index = setup
    router, verifier = Router([suggestion()]), Verifier()
    result = run(RepairPipeline(router, index, store, verifier, no_rules), root)
    assert result["status"] == "ready_for_approval"
    assert result["approval_required"]
    assert [tier for tier, _ in router.calls] == ["fast"]
    assert verifier.contents == ["broken", "fixed"]
    assert (root / "app.js").read_text() == "broken"
    assert result["sources"][0]["url"].startswith("https://expressjs.com/")
    assert result["llm_calls"] == 1
    assert store.approve(result["id"])["status"] == "applied"
    assert (root / "app.js").read_text() == "fixed"
    with pytest.raises(ValueError):
        store.approve(result["id"])


def test_rebuild_failure_escalates_and_uses_clean_snapshot(setup):
    root, store, index = setup
    router, verifier = Router([suggestion("still broken"), suggestion()]), Verifier()
    result = run(RepairPipeline(router, index, store, verifier, no_rules), root)
    assert result["status"] == "ready_for_approval"
    assert [tier for tier, _ in router.calls] == ["fast", "primary"]
    second = json.loads(router.calls[1][1].prompt)
    assert second["files"]["app.js"] == "broken"
    assert second["previous_failure"]
    assert result["estimated_cost_usd"] == .02


@pytest.mark.parametrize("strategy,tiers", [("A", ["primary", "primary"]), ("B", ["primary", "primary"]),
                                          ("C", ["fast", "fast"]), ("D", ["fast", "primary"])])
def test_comparison_conditions_and_retry_budget(setup, strategy, tiers):
    root, store, index = setup
    refs = [] if strategy == "A" else ["express"]
    router = Router([suggestion("bad1", refs), suggestion("bad2", refs)])
    result = run(RepairPipeline(router, index, store, Verifier(), no_rules), root, strategy=strategy)
    assert result["status"] == "unresolved"
    assert not result["approval_required"]
    assert [tier for tier, _ in router.calls] == tiers
    payload = json.loads(router.calls[0][1].prompt)
    assert bool(payload["documents"]) == (strategy != "A")
    with pytest.raises(ValueError):
        store.approve(result["id"])


def test_rule_failures_do_not_call_model_or_retriever(setup):
    root, store, index = setup
    issue = SimpleNamespace(code="NODE_BUILD_ENTRY_MISSING", auto_fix=False, to_dict=lambda: {"code": "NODE_BUILD_ENTRY_MISSING"})
    router = Router([])
    index.search = lambda *_: pytest.fail("retrieval called before rules")
    p = RepairPipeline(router, index, store, Verifier(), lambda *a, **k: SimpleNamespace(errors=[issue]))
    result = run(p, root)
    assert result["status"] == "rule_action_required"
    assert not router.calls


def test_rules_are_fixed_in_copy_and_still_need_build(setup):
    root, store, index = setup
    issue = SimpleNamespace(code="known", auto_fix=True, to_dict=lambda: {"code": "known"})
    def fix(base, code):
        (base / "app.js").write_text("fixed")
    p = RepairPipeline(Router([]), index, store, Verifier(), lambda *a, **k: SimpleNamespace(errors=[issue]), fix)
    r = run(p, root)
    assert r["status"] == "ready_for_approval" and r["llm_calls"] == 0
    assert (root / "app.js").read_text() == "broken"


def test_cache_revalidates_and_does_not_call_model(setup):
    root, store, index = setup
    p = RepairPipeline(Router([suggestion()]), index, store, Verifier(), no_rules)
    first = run(p, root)
    p.router = Router([])
    second = run(p, root)
    assert second["cache_hit"] and second["llm_calls"] == 0
    assert second["status"] == "ready_for_approval"
    assert first["id"] != second["id"]
    assert len(p.verifier.contents) == 4


def test_cache_invalidates_on_context_change(setup):
    root, store, index = setup
    p = RepairPipeline(Router([suggestion(), suggestion()]), index, store, Verifier(), no_rules)
    run(p, root)
    (root / "package.json").write_text('{"name":"new-context"}')
    r = run(p, root)
    assert not r["cache_hit"] and r["llm_calls"] == 1


def test_approval_rejects_changes_even_in_an_unedited_file(setup):
    root, store, index = setup
    r = run(RepairPipeline(Router([suggestion()]), index, store, Verifier(), no_rules), root)
    (root / "Dockerfile").write_text("FROM scratch")
    with pytest.raises(ValueError, match="changed"):
        store.approve(r["id"])
    assert (root / "app.js").read_text() == "broken"


@pytest.mark.parametrize("stage", ["run", "ecs", "iam", "s3"])
def test_cloud_or_runtime_errors_are_never_approved_by_build_only(setup, stage):
    root, store, index = setup
    r = run(RepairPipeline(Router([suggestion()]), index, store, Verifier(), no_rules), root, stage=stage)
    assert r["status"] == "environment_verification_required"
    assert not r["approval_required"]
    with pytest.raises(ValueError):
        store.approve(r["id"])


def test_citation_hallucination_is_rejected_before_build(setup):
    root, store, index = setup
    verifier = Verifier()
    r = run(RepairPipeline(Router([suggestion(citations=["invented"])] * 2), index, store, verifier, no_rules), root)
    assert r["status"] == "unresolved"
    assert verifier.contents == ["broken"]
    assert "citation" in r["attempts"][0]["error"]


def test_no_evidence_or_already_passing_build_uses_no_llm(setup):
    root, store, _ = setup
    p = RepairPipeline(Router([]), KnowledgeIndex([]), store, Verifier(), no_rules)
    assert run(p, root)["status"] == "no_document_evidence"
    (root / "app.js").write_text("fixed")
    assert run(p, root)["status"] == "failure_not_reproduced"


def test_summarization_preserves_early_codes_ecs_reasons_and_masks_secrets():
    log = "npm error code ETARGET\nnpm error No matching version found for jsonwebtoken@^9.1.2.\n"
    log += "password=superprivate\n" + ("download complete\n" * 9000)
    log += 'stoppedReason: AccessDenied ecr:BatchGetImage\n'
    e = summarize(log)
    assert "ETARGET" in e.keywords and "AccessDenied" in e.keywords
    assert "jsonwebtoken@^9.1.2" in e.text and "ecr:BatchGetImage" in e.text
    assert "superprivate" not in e.text and len(e.text) <= 4000


def test_exact_code_and_semantic_only_retrieval():
    docs = [passage("npm", "ETARGET dependency package version missing"),
            passage("health", "probe initialization startup grace period")]
    def embed(texts):
        return [[0., 1.] if "starts slowly" in t or "probe" in t else [1., 0.] for t in texts]
    index = KnowledgeIndex(docs, embed)
    assert index.search("ETARGET", ["ETARGET"])[0].id == "npm"
    assert index.search("service starts slowly", [])[0].id == "health"
    assert index.mode == "hybrid"


def test_unreviewed_copied_documents_are_rejected(tmp_path):
    p = tmp_path / "corpus.json"
    p.write_text(json.dumps([{**passage().to_dict(), "kind": "excerpt", "license_status": "pending"}]))
    with pytest.raises(ValueError, match="license"):
        KnowledgeIndex.load(p)


@pytest.mark.parametrize("name", ["../outside", "/etc/passwd", "a/../../out", ".git/config", ".env", "C:\\file", "a/.npmrc"])
def test_unsafe_paths_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        safe_path(tmp_path, name)


def test_symlink_and_secret_files_not_in_context(tmp_path):
    (tmp_path / ".env").write_text("password=secret")
    assert not manifest(tmp_path)
    (tmp_path / "link.js").symlink_to(tmp_path / ".env")
    with pytest.raises(ValueError):
        manifest(tmp_path)


def test_docker_verifier_never_invokes_model_commands(tmp_path, monkeypatch):
    (tmp_path / "Dockerfile").write_text("FROM scratch")
    commands = []
    def fake(cmd, **kwargs):
        commands.append(cmd)
        assert kwargs["shell"] is False
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr("grounded_repair.workspace.subprocess.run", fake)
    assert not DockerVerifier()(tmp_path)["passed"]
    assert commands == [["docker", "build", "--progress=plain", "."]]
