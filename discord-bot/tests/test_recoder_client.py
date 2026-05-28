"""recoder_client.py — Core API 클라이언트 단위 테스트.

실제 HTTP 호출은 하지 않고, httpx.MockTransport 로 가짜 응답을 주입한다.
모든 메서드가 올바른 URL/메서드/페이로드를 전송하는지 검증.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import recoder_client
from recoder_client import (
    GuildNotConfiguredError,
    RecoderClient,
    get_client_for_guild,
)


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _mock_transport(handler):
    """httpx.MockTransport 를 RecoderClient 의 httpx.AsyncClient 모든 호출에
    주입하는 monkeypatch helper. 사용자는 handler(request)->Response 함수만
    작성하면 된다.
    """
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    return transport, _patched_init


# ── 테스트 ───────────────────────────────────────────────────────────────────

def test_get_client_for_guild_raises_when_not_configured(temp_db):
    """설정 없으면 GuildNotConfiguredError 발생."""
    with pytest.raises(GuildNotConfiguredError):
        get_client_for_guild(42)


def test_get_client_for_guild_returns_client(temp_db):
    import guild_store
    guild_store.set_api(42, "https://core.example.com", "tok")

    c = get_client_for_guild(42)
    assert isinstance(c, RecoderClient)
    assert c._base == "https://core.example.com"
    assert c._headers["X-Session-Token"] == "tok"


def test_client_strips_trailing_slash():
    c = RecoderClient(base="https://core.example.com/", token="x")
    assert c._base == "https://core.example.com"


def test_preflight_sends_correct_payload(monkeypatch):
    """preflight() 가 올바른 URL/JSON 으로 POST 하는지."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"ok": True, "checks": []})

    transport, patched = _mock_transport(handler)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    c = RecoderClient("https://core", "tk")
    result = asyncio.run(c.preflight("cluster-a", "svc-x", region="ap-northeast-2"))

    assert result == {"ok": True, "checks": []}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://core/api/preflight/run"
    assert captured["headers"]["x-session-token"] == "tk"
    assert captured["headers"]["content-type"] == "application/json"
    # httpx 는 JSON 을 공백 없이 직렬화한다 — separators=(',', ':')
    assert '"cluster":"cluster-a"' in captured["body"]
    assert '"service":"svc-x"' in captured["body"]
    assert '"region":"ap-northeast-2"' in captured["body"]


def test_code_maps_prompt_to_terminal_output(monkeypatch):
    """code() 가 prompt 를 Core 의 terminal_output 필드로 매핑하는지 (예전 버그)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"analysis": "ok"})

    transport, patched = _mock_transport(handler)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    c = RecoderClient("https://core", "tk")
    asyncio.run(c.code(prompt="ImportError: foo", project_path="."))

    # workspace_path 가 빈 문자열로 폴백되는지
    assert '"workspace_path":""' in captured["body"]
    # prompt 가 terminal_output 에 들어갔는지
    assert '"terminal_output":"ImportError: foo"' in captured["body"]


def test_code_preserves_explicit_project_path(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={})

    transport, patched = _mock_transport(handler)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    c = RecoderClient("https://core", "tk")
    asyncio.run(c.code(prompt="x", project_path="/Users/me/proj"))
    assert '"workspace_path":"/Users/me/proj"' in captured["body"]


def test_rollback_omits_target_revision_when_none(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={})

    transport, patched = _mock_transport(handler)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    c = RecoderClient("https://core", "tk")
    asyncio.run(c.rollback("c", "s", target_revision=None))
    assert "target_revision" not in captured["body"]

    asyncio.run(c.rollback("c", "s", target_revision=7))
    assert '"target_revision":7' in captured["body"]


def test_status_url(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        return httpx.Response(200, json={"status": "ok"})

    transport, patched = _mock_transport(handler)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    c = RecoderClient("https://core", "tk")
    result = asyncio.run(c.status())
    assert result == {"status": "ok"}
    assert captured["method"] == "GET"
    assert captured["url"].startswith("https://core/api/health")


def test_status_with_session_id(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={})

    transport, patched = _mock_transport(handler)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    c = RecoderClient("https://core", "tk")
    asyncio.run(c.status(session_id="sess-1"))
    assert "session_id=sess-1" in captured["url"]


def test_http_error_propagates(monkeypatch):
    """Core 가 4xx 를 주면 raise_for_status() 가 예외를 던지는지."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "validation"})

    transport, patched = _mock_transport(handler)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    c = RecoderClient("https://core", "tk")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(c.preflight("c", "s"))


def test_deploy_forecast_payload(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"weather": "CLEAR"})

    transport, patched = _mock_transport(handler)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    c = RecoderClient("https://core", "tk")
    result = asyncio.run(c.get_deploy_forecast(service="api", window_days=14))
    assert result == {"weather": "CLEAR"}
    assert '"service":"api"' in captured["body"]
    assert '"window_days":14' in captured["body"]
