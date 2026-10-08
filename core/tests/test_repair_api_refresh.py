import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import repair
from grounded_repair.knowledge import KnowledgeIndex
from grounded_repair.refresh import refresh
from grounded_repair.store import RepairStore


def test_pending_license_is_not_fetched_and_reviewed_excerpt_has_provenance(tmp_path):
    seed = {"id": "docker", "title": "COPY", "url": "https://docs.docker.com/reference/dockerfile/",
            "text": "Original summary", "version": "rolling", "reviewed_at": "2026-10-03",
            "kind": "authored_summary", "fulltext_status": "pending_review"}
    assert refresh([seed], lambda _: pytest.fail("unreviewed source fetched")) == [seed]
    reviewed = {**seed, "fulltext_status": "approved", "license_status": "reviewed",
                "license_url": "https://github.com/docker/docs/blob/main/LICENSE", "attribution": "Docker documentation"}
    result = refresh([reviewed], lambda _: "<nav><p>menu</p></nav><main><h1>COPY</h1><p>Build context source.</p></main>")
    assert all(r["kind"] == "excerpt" and r["fetched_at"] and r["attribution"] for r in result)
    assert all(r["text"] != "menu" for r in result)
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps(result))
    assert KnowledgeIndex.load(corpus).passages[0].attribution == "Docker documentation"


def test_cannot_turn_on_crawl_without_license_review():
    with pytest.raises(ValueError, match="license"):
        refresh([{"id": "bad", "fulltext_status": "approved"}], lambda _: pytest.fail("fetched"))


def test_refresh_rejects_unofficial_url_before_network():
    with pytest.raises(ValueError, match="official"):
        refresh([{"id": "bad", "url": "http://169.254.169.254/", "fulltext_status": "approved",
                  "license_status": "reviewed", "license_url": "some-license", "attribution": "x",
                  "reviewed_at": "2026-10-03"}], lambda _: pytest.fail("fetched"))


def test_api_prepare_limits_and_approval_gate(tmp_path, monkeypatch):
    store = RepairStore(tmp_path / "runs.db")
    store.save({"id": "failed", "status": "unresolved"})
    monkeypatch.setattr(repair, "_store", lambda: store)
    app = FastAPI()
    app.include_router(repair.router)
    with TestClient(app) as client:
        assert client.post("/api/repair/prepare", json={"workspace_path": "/a", "log": "x" * 1_000_001}).status_code == 422
        assert client.post("/api/repair/failed/approve").status_code == 409
        assert client.get("/api/repair/missing").status_code == 404


def test_lambda_does_not_overwrite_previous_corpus_after_failed_fetch(monkeypatch):
    from grounded_repair import refresh_lambda
    class S3:
        puts = []
        def get_object(self, **kw):
            import io
            return {"Body": io.BytesIO(b'[{"id":"bad","fulltext_status":"approved"}]')}
        def put_object(self, **kw):
            self.puts.append(kw)
    client = S3()
    monkeypatch.setenv("DOCUMENT_BUCKET", "test-docs")
    monkeypatch.setattr("boto3.client", lambda _: client)
    with pytest.raises(ValueError):
        refresh_lambda.handler({}, None)
    assert not client.puts
