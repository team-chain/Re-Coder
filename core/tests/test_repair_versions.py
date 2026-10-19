"""Version-incompatible documents must never reach the repair model."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from grounded_repair.knowledge import KnowledgeIndex, Passage
from grounded_repair.pipeline import RepairPipeline
from grounded_repair.refresh import refresh
from grounded_repair.store import RepairStore
from grounded_repair.versions import declared_major, project_versions
from grounded_repair.workspace import manifest


def document(name: str, majors: list[int]) -> Passage:
    return Passage(
        name,
        "Express route syntax",
        "https://expressjs.com/en/guide/migrating-5/",
        "Missing parameter name wildcard route",
        "source-revision-sha",
        "2026-10-20",
        applies_to={"express": majors},
    )


class Embedding:
    identity = "deterministic-test-embedding"

    def __call__(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.parametrize("embed", [None, Embedding()])
def test_filters_before_top_k_in_both_retrievers(embed):
    wrong = [document(f"wrong-{i}", [5]) for i in range(8)]
    correct = document("compatible", [4])
    index = KnowledgeIndex(wrong + [correct], embed)
    assert index.search(
        "Missing parameter name", ["wildcard"], 1, project_versions={"express": 4}
    ) == [correct]
    assert index.search("Missing parameter name", [], project_versions={"express": 6}) == []


def test_unknown_versions_and_unversioned_docs_are_retained():
    tagged = document("tagged", [5])
    generic = replace(tagged, id="generic", applies_to={})
    index = KnowledgeIndex([tagged, generic])
    assert len(index.search("wildcard", [], project_versions={})) == 2
    assert index.search("wildcard", [], project_versions={"express": 4}) == [generic]
    assert index.version != KnowledgeIndex([replace(tagged, applies_to={}), generic]).version


@pytest.mark.parametrize(
    "value", [None, [], {"express": []}, {"express": [True]}, {"express": [-1]}, {"express": "5"}]
)
def test_rejects_malformed_applicability(value):
    with pytest.raises(ValueError, match="applicability"):
        KnowledgeIndex([replace(document("bad", [5]), applies_to=value)])


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("^5.1.0", 5),
        ("~4.21.0", 4),
        ("4.x", 4),
        (" 5 ", 5),
        ("^0.2.1", 0),
        (">=4", None),
        ("4 || 5", None),
        ("*", None),
        ("latest", None),
        ("workspace:*", None),
        ("npm:express@5", None),
        ("5.0.0-beta.1", None),
        (None, None),
        ("4.x.2", None),
        ("4.01.0", None),
    ],
)
def test_only_unambiguous_stable_major_ranges(spec, expected):
    assert declared_major(spec) == expected


@pytest.mark.parametrize("other", ["^5.0.0", ">=4", "workspace:*"])
def test_monorepo_conflicts_are_unknown(tmp_path, other):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"express": "^4.0.0"}}), encoding="utf-8"
    )
    child = tmp_path / "server"
    child.mkdir()
    (child / "package.json").write_text(
        json.dumps({"dependencies": {"express": other}}), encoding="utf-8"
    )
    assert project_versions(tmp_path, manifest(tmp_path), {"express", "vite"}) == {}


def test_bom_and_dependency_fields(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"express": "^5.0.0"},
                "devDependencies": {"vite": "~6.1.0"},
                "optionalDependencies": {"express": "5.x"},
            }
        ),
        encoding="utf-8-sig",
    )
    assert project_versions(tmp_path, manifest(tmp_path), {"express", "vite"}) == {
        "express": 5,
        "vite": 6,
    }


@pytest.mark.parametrize("contents", ["[]", "{", '{"dependencies": []}', " " * 128_001])
def test_invalid_manifest_does_not_create_false_certainty(tmp_path, contents):
    (tmp_path / "package.json").write_text(contents, encoding="utf-8")
    assert project_versions(tmp_path, manifest(tmp_path), {"express", "vite"}) == {}


def test_pipeline_does_not_send_incompatible_evidence_to_model(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text('{"dependencies":{"express":"^4.21.0"}}', encoding="utf-8")
    (root / "app.js").write_text("broken", encoding="utf-8")

    class Router:
        async def call_repair(self, *args, **kwargs):
            pytest.fail("Model must not receive incompatible-only evidence")

    class Verifier:
        identity = "failed-build-test"

        def __call__(self, workspace):
            return {"passed": False, "available": True}

    pipeline = RepairPipeline(
        Router(),
        KnowledgeIndex([document("express5", [5])]),
        RepairStore(tmp_path / "runs.db"),
        Verifier(),
        readiness=lambda *a, **k: SimpleNamespace(errors=[]),
    )
    result = asyncio.run(pipeline.run(str(root), "Missing parameter name wildcard"))
    assert result["status"] == "no_document_evidence"
    assert result["project_versions"] == {"express": 4}
    assert result["llm_calls"] == 0
    assert (root / "app.js").read_text(encoding="utf-8") == "broken"


def test_catalog_and_refresh_preserve_applicability():
    root = Path(__file__).resolve().parents[2]
    for path in (
        root / "core/grounded_repair/sources.json",
        root / "benchmarks/repair/corpus.json",
    ):
        index = KnowledgeIndex.load(path, model_path="")
        express = [p for p in index.passages if "migrating-5" in p.url]
        assert express and all(p.applies_to == {"express": [5]} for p in express)
    entries = json.loads(
        (root / "benchmarks/repair/source-manifest.json").read_text(encoding="utf-8")
    )
    source = next(entry for entry in entries if entry["id"] == "express-migration")
    refreshed = refresh([source], fetcher=lambda _: "# Path route matching\n\nNew route syntax.")
    assert all(p["applies_to"] == {"express": [5]} for p in refreshed)
    assert refreshed[0]["version"] != source["version"]
