"""middleware/auth.py — Hybrid 인증 (§6.1.4) 단위 테스트."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import guild_store
from middleware import auth


# ── 더미 Interaction 생성 헬퍼 ──────────────────────────────────────────────

def _make_member(user_id: int, role_ids: list[int], manage_guild: bool):
    perms = SimpleNamespace(manage_guild=manage_guild)
    roles = [SimpleNamespace(id=r) for r in role_ids]
    return SimpleNamespace(
        id=user_id,
        guild_permissions=perms,
        roles=roles,
    )


def _make_interaction(
    guild_id: int | None,
    user_id: int,
    role_ids: list[int] | None = None,
    manage_guild: bool = False,
    member_in_cache: bool = True,
):
    """is_allowed() 가 보는 최소한의 Interaction 인터페이스를 stub."""
    user = SimpleNamespace(id=user_id, name=f"user-{user_id}")

    if guild_id is None:
        return SimpleNamespace(guild=None, user=user)

    member = _make_member(user_id, role_ids or [], manage_guild)
    guild = SimpleNamespace(
        id=guild_id,
        get_member=lambda uid: member if member_in_cache and uid == user_id else None,
    )
    return SimpleNamespace(guild=guild, user=user)


# ── is_allowed ──────────────────────────────────────────────────────────────

def test_dm_is_rejected(temp_db):
    """DM (guild=None) 은 무조건 거부."""
    inter = _make_interaction(guild_id=None, user_id=1)
    assert auth.is_allowed(inter) is False


def test_server_admin_always_allowed(temp_db):
    """manage_guild 권한자는 화이트리스트/역할 무관하게 허용."""
    inter = _make_interaction(guild_id=10, user_id=1, manage_guild=True)
    assert auth.is_allowed(inter) is True


def test_user_in_whitelist_allowed(temp_db):
    """§6.1.4 user_id 화이트리스트 — 1차 게이트."""
    guild_store.add_user(10, 1)
    inter = _make_interaction(guild_id=10, user_id=1)
    assert auth.is_allowed(inter) is True


def test_user_with_allowed_role_allowed(temp_db):
    """역할 기반 보조 게이트."""
    guild_store.add_role(10, 999)
    inter = _make_interaction(guild_id=10, user_id=1, role_ids=[999])
    assert auth.is_allowed(inter) is True


def test_no_whitelist_no_role_denied(temp_db):
    inter = _make_interaction(guild_id=10, user_id=1, role_ids=[123])
    # role 123 은 허용 목록에 없음
    assert auth.is_allowed(inter) is False


def test_cache_miss_falls_back_to_user_whitelist(temp_db):
    """member 캐시 미스 시 user_id 화이트리스트만 단독 확인."""
    guild_store.add_user(10, 1)
    inter = _make_interaction(guild_id=10, user_id=1, member_in_cache=False)
    assert auth.is_allowed(inter) is True


def test_cache_miss_without_whitelist_denied(temp_db):
    inter = _make_interaction(guild_id=10, user_id=1, member_in_cache=False)
    assert auth.is_allowed(inter) is False


# ── is_guild_configured ─────────────────────────────────────────────────────

def test_is_guild_configured_false_without_api(temp_db):
    assert auth.is_guild_configured(10) is False


def test_is_guild_configured_true_after_set_api(temp_db):
    guild_store.set_api(10, "https://example.com", "tk")
    assert auth.is_guild_configured(10) is True


# ── _get_deny_message ───────────────────────────────────────────────────────

def test_deny_message_for_dm(temp_db):
    inter = _make_interaction(guild_id=None, user_id=1)
    msg = auth._get_deny_message(inter)
    assert "DM" in msg


def test_deny_message_for_unconfigured_guild(temp_db):
    inter = _make_interaction(guild_id=10, user_id=1)
    msg = auth._get_deny_message(inter)
    assert "setup api" in msg


def test_deny_message_for_configured_but_unauthorized(temp_db):
    guild_store.set_api(10, "https://x", "t")
    inter = _make_interaction(guild_id=10, user_id=1)
    msg = auth._get_deny_message(inter)
    assert "접근 거부" in msg or "거부" in msg
    assert "setup user" in msg or "setup role" in msg


# ── get_whitelist_count ─────────────────────────────────────────────────────

def test_whitelist_count_per_guild(temp_db):
    guild_store.add_user(10, 1)
    guild_store.add_user(10, 2)
    guild_store.add_user(20, 3)

    assert auth.get_whitelist_count(10) == 2
    assert auth.get_whitelist_count(20) == 1
    assert auth.get_whitelist_count(999) == 0


def test_whitelist_count_total(temp_db):
    guild_store.add_user(10, 1)
    guild_store.add_user(10, 2)
    guild_store.add_user(20, 3)
    assert auth.get_whitelist_count(None) == 3
