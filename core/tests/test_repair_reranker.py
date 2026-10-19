"""Optional learned ordering must preserve evidence and resource boundaries."""

from __future__ import annotations

import json
import runpy
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from grounded_repair.knowledge import KnowledgeIndex, Passage
from grounded_repair.reranker import LocalReranker


def passage(name: str, text: str = "wildcard route syntax") -> Passage:
    return Passage(
        name, "Express", "https://expressjs.com/en/guide/migrating-5/", text, "rev", "2026-10-20"
    )


class Reranker:
    identity = "test-reranker"

    def __init__(self, scores=None):
        self.scores = scores
        self.calls = []

    def __call__(self, query, texts):
        self.calls.append((query, texts))
        return self.scores if self.scores is not None else list(range(len(texts)))


def test_optional_reranker_changes_order_and_cache_identity():
    docs = [passage("a"), passage("b")]
    base, reranked = KnowledgeIndex(docs), KnowledgeIndex(docs, reranker=Reranker())
    assert [p.id for p in base.search("wildcard", [])] == ["a", "b"]
    assert [p.id for p in reranked.search("wildcard", [])] == ["b", "a"]
    assert base.version != reranked.version
    assert base.mode == reranked.mode == "keyword_only"
    assert base.retrieval_profile["reranker"] == "none"


def test_reranker_cannot_remove_exact_error_priority_or_restore_wrong_version():
    docs = [
        passage("exact", "ETARGET wildcard route syntax"),
        passage("generic"),
        replace(passage("wrong", "incompatible wildcard route"), applies_to={"express": [5]}),
    ]
    model = Reranker()
    index = KnowledgeIndex(docs, reranker=model)
    result = index.search("ETARGET wildcard", ["ETARGET"], project_versions={"express": 4})
    assert [p.id for p in result] == ["exact", "generic"]
    assert not any("incompatible" in text for text in model.calls[0][1])


def test_candidate_count_is_bounded_and_ties_preserve_base_order():
    docs = [passage(f"p-{i:02}") for i in range(50)]
    model = Reranker([1.0] * 20)
    index = KnowledgeIndex(docs, reranker=model)
    assert index.search("wildcard", [], 4) == docs[:4]
    assert len(model.calls[0][1]) == 20


@pytest.mark.parametrize(
    "scores",
    [[], [1.0], [float("nan"), 1.0], [1.0, float("inf")]],
    ids=["empty", "short", "nan", "infinite"],
)
def test_invalid_scores_fail_explicitly(scores):
    index = KnowledgeIndex([passage("a"), passage("b")], reranker=Reranker(scores))
    with pytest.raises(ValueError, match="invalid relevance"):
        index.search("wildcard", [])


@pytest.mark.parametrize("top_k", [0, -1, 21])
def test_invalid_output_limits_are_rejected(top_k):
    with pytest.raises(ValueError, match="top_k"):
        KnowledgeIndex([passage("a")]).search("wildcard", [], top_k)


def test_zero_or_single_candidate_needs_no_inference():
    model = Reranker()
    index = KnowledgeIndex([passage("a")], reranker=model)
    assert index.search("absent", []) == []
    assert index.search("wildcard", [])[0].id == "a"
    assert model.calls == []


def test_local_loader_never_downloads_or_executes_remote_model_code(tmp_path, monkeypatch):
    captured = {}

    class Model:
        num_labels = 1

        def __init__(self, path, **kwargs):
            captured.update(kwargs)

        def predict(self, pairs, **kwargs):
            captured["pairs"] = pairs
            return [0.2] * len(pairs)

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=Model))
    (tmp_path / "model.safetensors").write_bytes(b"fixture")
    model = LocalReranker(str(tmp_path))
    assert captured["local_files_only"] is True
    assert captured["trust_remote_code"] is False
    assert captured["model_kwargs"] == {"use_safetensors": True}
    assert model("q" * 5000, ["p" * 4000]) == [0.2]
    assert list(map(len, captured["pairs"][0])) == [4000, 2800]
    with pytest.raises(ValueError, match="candidate limit"):
        model("query", ["text"] * 21)
    Model.num_labels = 2
    with pytest.raises(ValueError, match="exactly one"):
        LocalReranker(str(tmp_path))


def test_remote_or_pickle_only_models_are_rejected_before_loading(tmp_path):
    with pytest.raises(ValueError, match="local safetensors"):
        LocalReranker("BAAI/not-a-local-model")
    (tmp_path / "pytorch_model.bin").write_bytes(b"not allowed")
    with pytest.raises(ValueError, match="local safetensors"):
        LocalReranker(str(tmp_path))


def test_environment_opt_in_can_be_explicitly_disabled(tmp_path, monkeypatch):
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps([passage("a").to_dict()]), encoding="utf-8")
    requested = []

    def load_model(path):
        requested.append(path)
        return Reranker()

    monkeypatch.setattr("grounded_repair.reranker.LocalReranker", load_model)
    monkeypatch.setenv("RECODER_REPAIR_RERANKER_MODEL", "explicit-local-directory")
    assert KnowledgeIndex.load(path, model_path="").reranker is not None
    assert KnowledgeIndex.load(path, model_path="", reranker_path="").reranker is None
    assert requested == ["explicit-local-directory"]


def test_runtime_config_changes_invalidate_cached_index(tmp_path, monkeypatch):
    from grounded_repair import config

    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([passage("a").to_dict()]), encoding="utf-8")
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"corpus": str(corpus)}), encoding="utf-8")
    monkeypatch.setenv("RECODER_REPAIR_CONFIG", str(config_file))
    monkeypatch.delenv("RECODER_REPAIR_RERANKER_MODEL", raising=False)
    monkeypatch.setenv("RECODER_REPAIR_EMBEDDING_MODEL", "")
    monkeypatch.setenv("RECODER_REPAIR_CORPUS", str(corpus))
    monkeypatch.setattr("grounded_repair.reranker.LocalReranker", lambda _: Reranker())
    try:
        base = config.load_index()
        monkeypatch.setenv("RECODER_REPAIR_RERANKER_MODEL", "local-model")
        ranked = config.load_index()
        assert ranked is not base and ranked.reranker is not None
        assert config.load_index() is ranked
    finally:
        config._index.cache_clear()


def test_comparison_counts_misses_and_negative_control_separately():
    # Repository scripts also run when pytest's working directory is core/ in CI.
    script = Path(__file__).resolve().parents[2] / "scripts/compare_repair_reranker.py"
    calculate = runpy.run_path(str(script))["metrics"]
    rows = [
        {"expected": ["doc"], "baseline": {"ids": ["wrong", "doc:section"], "latency_ms": [10]}},
        {"expected": ["doc"], "baseline": {"ids": ["document"], "latency_ms": [20]}},
        {"expected": [], "forbidden": ["bad"], "baseline": {"ids": ["bad:1"], "latency_ms": [30]}},
    ]
    result = calculate(rows, "baseline")
    assert result["positive_queries"] == 2
    assert result["hit_at_1"] == 0
    assert result["hit_at_4"] == 0.5
    assert result["mrr_at_4"] == 0.25
    assert result["forbidden_hits"] == 1
    assert result["median_ms"] == 20 and result["p95_ms"] == 30
