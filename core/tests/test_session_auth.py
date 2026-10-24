"""Localhost is not an authentication boundary; no real AWS/Docker calls."""
import asyncio
import os
import stat

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from api.middleware.auth import SessionTokenMiddleware

TOKEN = "test-session-token"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "192.0.2.1"])
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("provided", [None, "wrong-token", TOKEN])
def test_every_client_requires_token_before_handler(host, method, provided):
    app = FastAPI()
    app.state.session_token = TOKEN
    app.add_middleware(SessionTokenMiddleware)
    calls = []

    async def handler():
        calls.append(True)
        return {"ok": True}

    app.add_api_route("/protected", handler, methods=[method])
    client = TestClient(app, client=(host, 1234))
    response = client.request(method, "/protected", headers={"X-Session-Token": provided} if provided else {})
    assert response.status_code == (200 if provided == TOKEN else 401)
    assert len(calls) == (1 if provided == TOKEN else 0)
    assert TOKEN not in response.text


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/aws/connect"), ("POST", "/api/aws/configure"),
    ("POST", "/api/aws/clear"), ("POST", "/api/aws/permissions/check"),
    ("GET", "/api/aws/status"), ("POST", "/api/github/token"),
    ("POST", "/api/github/logout"), ("GET", "/api/github/status"),
    ("POST", "/api/diagnostics/run"), ("GET", "/api/diagnostics"),
    ("GET", "/api/status"), ("GET", "/api/ready"),
    ("GET", "/api/cost"), ("GET", "/api/project"),
    ("GET", "/workbench/events"), ("GET", "/workbench/state"),
    ("GET", "/api/deploy/history"), ("POST", "/api/deploy/ecs"),
    ("POST", "/api/deploy/s3/stream"), ("POST", "/api/deploy/execute/stream"), ("POST", "/api/shutdown"),
    ("GET", "/docs"), ("GET", "/openapi.json"),
    ("POST", "/api/health"), ("GET", "/api/health/"),
    ("GET", "/api/health/anything"), ("GET", "/api/token"),
])
def test_real_app_does_not_exempt_sensitive_or_similar_paths(method, path):
    app = main.create_app()
    app.state.session_token = TOKEN
    response = TestClient(app, client=("127.0.0.1", 1234)).request(method, path)
    assert response.status_code == 401
    assert response.json() == {"detail": "Missing X-Session-Token header."}


def test_health_bootstrap_and_authenticated_polling():
    app = main.create_app()
    client = TestClient(app, client=("127.0.0.1", 1234))
    response = client.get("/api/health")
    assert response.status_code == 200
    assert set(response.json()) == {"status", "version", "uptime_seconds", "port"}
    assert client.get("/api/status").status_code == 503  # fail closed before startup
    app.state.session_token = TOKEN
    assert client.get("/api/status", headers={"X-Session-Token": TOKEN}).status_code == 200
    # Malformed non-ASCII header must be 401, not compare_digest's TypeError/500.
    assert client.get("/api/status", headers=[(b"X-Session-Token", b"\xff")]).status_code == 401


def test_cors_preflight_does_not_authorize_or_invoke_mutation():
    app = main.create_app()
    app.state.session_token = TOKEN
    calls = []

    @app.post("/auth-probe")
    async def probe():
        calls.append(True)
        return {"ok": True}

    client = TestClient(app, client=("127.0.0.1", 1234))
    origin = "vscode-webview://test-extension"
    response = client.options("/auth-probe", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "X-Session-Token,Content-Type",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert calls == []
    response = client.post("/auth-probe", headers={"Origin": origin})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == origin
    assert calls == []
    assert client.post("/auth-probe", headers={"Origin": origin, "X-Session-Token": TOKEN}).status_code == 200
    assert len(calls) == 1
    assert client.post("/auth-probe", headers={"Origin": "https://untrusted.example", "X-Session-Token": TOKEN}).status_code == 403
    assert client.post("/auth-probe", headers={"Sec-Fetch-Site": "cross-site", "X-Session-Token": TOKEN}).status_code == 403
    assert client.options("/auth-probe", headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "POST"}).status_code == 400
    assert len(calls) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode check")
def test_persisted_token_is_private_on_create_and_upgrade(tmp_path):
    path = tmp_path / ".session_token"
    main._persist_session_token(path, TOKEN)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    main._persist_session_token(path, "shorter")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "shorter"


def test_restart_rotates_session_token_but_keeps_ci_token(monkeypatch, tmp_path):
    """Exercise lifespan without sockets, AWS, real locks or user state.

    The all-powerful session token must change on every start (it used to be
    persisted so a GitHub Actions secret kept working). The CI token persists,
    and a stale legacy ``.session_token`` file is removed, never reused.
    """
    from pathlib import Path
    import sys
    from types import SimpleNamespace

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("SESSION_TOKEN", raising=False)
    monkeypatch.setenv("RECODER_RELAY_ENABLED", "false")
    monkeypatch.setitem(sys.modules, "observability", SimpleNamespace(observability=SimpleNamespace(initialize=lambda: None)))
    legacy = tmp_path / ".recoder" / ".session_token"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy-persisted-token-that-must-not-be-reused-0000", encoding="utf-8")
    singleton = main.CoreSingleton
    monkeypatch.setattr(singleton, "acquire_lock", lambda pid: True)
    monkeypatch.setattr(singleton, "find_available_port", lambda: 17894)
    monkeypatch.setattr(singleton, "read_runtime", lambda: None)
    writes = []
    monkeypatch.setattr(singleton, "write_runtime", lambda **kw: writes.append(kw))
    monkeypatch.setattr(singleton, "set_file_permissions", lambda path: None)
    monkeypatch.setattr(singleton, "remove_window", lambda pid: True)
    monkeypatch.setattr(singleton, "release_lock", lambda pid: None)
    ci_tokens = []

    async def run():
        for _ in range(2):
            app = main.create_app()
            async with main.lifespan(app):
                client = TestClient(app)
                assert client.get("/api/status").status_code == 401
                assert client.get("/api/status", headers={"X-Session-Token": app.state.session_token}).status_code == 200
                ci_tokens.append(app.state.ci_token)
                # The CI token opens nothing but the Preflight gate.
                assert client.get("/api/status", headers={"X-Session-Token": app.state.ci_token}).status_code == 401
                shown = client.get("/api/session/ci-token", headers={"X-Session-Token": app.state.session_token})
                assert shown.status_code == 200 and shown.json()["token"] == app.state.ci_token
                assert shown.json()["secret_name"] == "RECODER_CI_TOKEN"

    asyncio.run(run())
    assert len(writes) == 2
    assert writes[0]["token"] != writes[1]["token"], "session token must rotate on every start"
    assert all(len(w["token"]) >= 32 for w in writes)
    assert "legacy-persisted" not in writes[0]["token"] and not legacy.exists()
    assert ci_tokens[0] == ci_tokens[1] and len(ci_tokens[0]) >= 32
    assert ci_tokens[0] not in {w["token"] for w in writes}
    assert (tmp_path / ".recoder" / "ci_token").read_text(encoding="utf-8") == ci_tokens[0]
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / ".recoder" / "ci_token").stat().st_mode) == 0o600


@pytest.mark.parametrize("method,path,expected", [
    ("POST", "/workbench/preflight/run", 200),
    ("GET", "/workbench/preflight/run", 401),
    ("POST", "/workbench/preflight/run/", 401),
    ("POST", "/workbench/mode", 401),
    ("POST", "/api/deploy/ecs", 401),
    ("GET", "/api/session/ci-token", 401),
])
def test_ci_token_is_accepted_only_for_preflight_run(method, path, expected):
    app = FastAPI()
    app.state.session_token = TOKEN
    app.state.ci_token = "ci-token-" + "x" * 32
    app.add_middleware(SessionTokenMiddleware)
    calls = []

    async def handler():
        calls.append(path)
        return {"ok": True}

    app.add_api_route(path, handler, methods=[method])
    client = TestClient(app)
    response = client.request(method, path, headers={"X-Session-Token": app.state.ci_token})
    assert response.status_code == expected
    assert len(calls) == (1 if expected == 200 else 0)
    # The session token still works everywhere.
    assert client.request(method, path, headers={"X-Session-Token": TOKEN}).status_code == 200


def test_generated_workflow_uses_ci_token_secret(monkeypatch, tmp_path):
    import infra_agent
    monkeypatch.setenv("RECODER_GHA_PREFLIGHT", "1")
    proposal = infra_agent.generate_github_actions(None, str(tmp_path))
    assert "${{ secrets.RECODER_CI_TOKEN }}" in proposal.content
    assert "RECODER_SESSION_TOKEN" not in proposal.content
    assert "/workbench/preflight/run" in proposal.content
    assert "RECODER_CI_TOKEN" in proposal.required_secrets
    assert "RECODER_SESSION_TOKEN" not in proposal.required_secrets
