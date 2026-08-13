"""
discord 브리지 보안 — Codex P1 회귀:
1. GitHub 웹훅 서명 미설정 시 거부(fail-closed).
2. /link 소유 증명 — student_id 만으로는 브리지 수신 자격을 못 얻는다.
3. 등록 API 무키 상태는 루프백만 허용.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_BOT = Path(__file__).resolve().parents[1]
if str(_BOT) not in sys.path:
    sys.path.insert(0, str(_BOT))


def test_github_webhook_rejects_when_secret_unset(monkeypatch):
    import api_server
    monkeypatch.setattr(api_server, "GITHUB_WEBHOOK_SECRET", "")
    # 서명이 있어도 시크릿이 없으면 검증 불가 → 거부
    assert api_server._verify_github_signature(b"{}", "sha256=deadbeef") is False


def test_github_webhook_verifies_when_secret_set(monkeypatch):
    import hashlib
    import hmac
    import api_server
    secret = "s3cr3t"
    monkeypatch.setattr(api_server, "GITHUB_WEBHOOK_SECRET", secret)
    body = b'{"ok":1}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert api_server._verify_github_signature(body, sig) is True
    assert api_server._verify_github_signature(body, "sha256=wrong") is False


def test_link_requires_full_token_for_ownership():
    """[Codex P1 회귀] student_id 만으로는 소유를 증명 못 한다 — 브리지가
    등록 해시로 검증하고, 해시 없는 바인딩은 통과 못 시킨다."""
    import importlib
    import guild_store
    with tempfile.TemporaryDirectory() as td:
        guild_store.DB_PATH = Path(td) / "g.db"   # 격리 DB
        guild_store.init_db() if hasattr(guild_store, "init_db") else None
        full = "rcdr_stud42_abcSECRET"
        guild_store.set_binding(12345, "stud42", guild_store._token_hash(full))

        # 전체 토큰을 제시하면 검증 통과
        assert guild_store.verify_student_secret("stud42", full) is True
        # 다른 토큰/추측은 거부
        assert guild_store.verify_student_secret("stud42", "rcdr_stud42_WRONG") is False
        # 소유 증명(해시) 없이 sid 만 등록된 경우 거부
        guild_store.set_binding(999, "victim", "")
        assert guild_store.verify_student_secret("victim", "anything") is False


def test_registration_auth_denies_external_when_keyless(monkeypatch):
    """무키 상태에서 루프백 외 요청은 거부된다."""
    import api_server
    monkeypatch.setattr(api_server, "REGISTRATION_KEY", "")

    class _Req:
        def __init__(self, remote, headers=None):
            self.remote = remote
            self.headers = headers or {}
    assert api_server._check_auth(_Req("127.0.0.1")) is True
    assert api_server._check_auth(_Req("203.0.113.7")) is False
