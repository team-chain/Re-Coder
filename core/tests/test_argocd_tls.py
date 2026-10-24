"""ArgoCD 호출은 TLS 인증서를 항상 검증한다 (3.0.2 — verify=False 제거)."""
from __future__ import annotations

import asyncio

import pytest

from core.agents import argocd_agent as mod
from core.schemas import ArgoSyncPhase, ArgoSyncRequest


class _Capture:
    seen: list = []

    def __init__(self, *args, **kwargs):
        _Capture.seen.append(kwargs.get("verify", "MISSING"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *a, **k):
        raise RuntimeError("no network in tests")

    async def post(self, *a, **k):
        raise RuntimeError("no network in tests")


@pytest.fixture
def captured(monkeypatch):
    _Capture.seen = []
    monkeypatch.setattr(mod.httpx, "AsyncClient", _Capture)
    monkeypatch.delenv(mod.CA_BUNDLE_ENV, raising=False)
    return _Capture.seen


def _req():
    return ArgoSyncRequest(project_id="p", app_name="app", argocd_server="argocd.example", argocd_token="t")


def test_every_argocd_client_verifies_tls_by_default(captured):
    agent = mod.ArgoCDAgent()
    asyncio.run(agent.sync(_req()))
    with pytest.raises(RuntimeError):
        asyncio.run(agent.get_app_status("app", "argocd.example", "t"))
    asyncio.run(agent.rollback_app("app", "argocd.example", "t", 1))
    assert captured == [True, True, True]


def test_ca_bundle_from_config_is_used(captured, monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setenv(mod.CA_BUNDLE_ENV, str(ca))
    asyncio.run(mod.ArgoCDAgent().sync(_req()))
    assert captured == [str(ca)]


def test_missing_ca_bundle_fails_closed_instead_of_disabling_tls(captured, monkeypatch, tmp_path):
    monkeypatch.setenv(mod.CA_BUNDLE_ENV, str(tmp_path / "nope.pem"))
    record = asyncio.run(mod.ArgoCDAgent().sync(_req()))
    assert record.sync_phase == ArgoSyncPhase.SYNC_FAILED and "CA 파일" in record.error_message
    assert captured == [], "검증 설정이 틀리면 연결하지 않는다(verify=False 로 떨어지지 않음)"
