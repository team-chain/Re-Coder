"""guild_store.py — 서버별 설정 영속화 단위 테스트."""

from __future__ import annotations

import pytest

import guild_store


def test_init_db_creates_tables(temp_db):
    """init_db() 가 4개 테이블을 모두 만들었는지 검사."""
    import sqlite3

    conn = sqlite3.connect(str(temp_db))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()

    names = {r[0] for r in rows}
    assert {"guild_config", "guild_channels", "guild_roles", "guild_users"} <= names


def test_set_get_api(temp_db):
    """set_api/get_api 라운드트립 + URL trailing slash 제거."""
    guild_store.set_api(1001, "https://example.com/", "token-abc")

    cfg = guild_store.get_api(1001)
    assert cfg == ("https://example.com", "token-abc")  # trailing slash stripped


def test_get_api_returns_none_for_unknown_guild(temp_db):
    assert guild_store.get_api(9999) is None


def test_set_api_upsert(temp_db):
    """동일 guild_id 에 두 번 set 하면 최신값으로 덮어쓰여야 한다."""
    guild_store.set_api(2002, "https://a.com", "t1")
    guild_store.set_api(2002, "https://b.com", "t2")
    assert guild_store.get_api(2002) == ("https://b.com", "t2")


def test_channel_crud(temp_db):
    guild_store.set_channel(3003, "deploy", 12345)
    guild_store.set_channel(3003, "incident", 67890)

    assert guild_store.get_channel(3003, "deploy") == 12345
    assert guild_store.get_channel(3003, "incident") == 67890
    assert guild_store.get_channel(3003, "standup") is None

    channels = guild_store.get_all_channels(3003)
    assert channels == {"deploy": 12345, "incident": 67890}


def test_channel_upsert(temp_db):
    guild_store.set_channel(4004, "deploy", 111)
    guild_store.set_channel(4004, "deploy", 222)
    assert guild_store.get_channel(4004, "deploy") == 222


def test_roles_add_remove_list(temp_db):
    guild_store.add_role(5005, 100)
    guild_store.add_role(5005, 200)
    guild_store.add_role(5005, 100)  # 중복 — INSERT OR IGNORE

    roles = guild_store.get_roles(5005)
    assert sorted(roles) == [100, 200]

    guild_store.remove_role(5005, 100)
    assert guild_store.get_roles(5005) == [200]


def test_user_whitelist(temp_db):
    """§6.1.4 user_id 화이트리스트 CRUD."""
    guild_store.add_user(6006, 7001, note="ops-lead")
    guild_store.add_user(6006, 7002)

    assert guild_store.is_user_allowed(6006, 7001) is True
    assert guild_store.is_user_allowed(6006, 7002) is True
    assert guild_store.is_user_allowed(6006, 9999) is False

    users = guild_store.list_users(6006)
    user_ids = {u[0] for u in users}
    assert user_ids == {7001, 7002}

    # note 가 정확히 저장되는지
    by_id = {u[0]: u[1] for u in users}
    assert by_id[7001] == "ops-lead"

    assert guild_store.get_user_whitelist_count(6006) == 2

    guild_store.remove_user(6006, 7001)
    assert guild_store.is_user_allowed(6006, 7001) is False
    assert guild_store.get_user_whitelist_count(6006) == 1


def test_user_whitelist_upsert_note(temp_db):
    guild_store.add_user(7777, 1, note="initial")
    guild_store.add_user(7777, 1, note="updated")

    users = guild_store.list_users(7777)
    assert len(users) == 1
    assert users[0][1] == "updated"


def test_delete_guild_cascades(temp_db):
    """delete_guild() 가 4개 테이블 전부에서 해당 guild 데이터를 지우는지."""
    g = 8008
    guild_store.set_api(g, "https://x", "t")
    guild_store.set_channel(g, "deploy", 1)
    guild_store.add_role(g, 10)
    guild_store.add_user(g, 100)

    guild_store.delete_guild(g)

    assert guild_store.get_api(g) is None
    assert guild_store.get_all_channels(g) == {}
    assert guild_store.get_roles(g) == []
    assert guild_store.list_users(g) == []


def test_guild_summary_shape(temp_db):
    g = 9009
    guild_store.set_api(g, "https://a", "t")
    guild_store.set_channel(g, "deploy", 1)
    guild_store.add_role(g, 1)
    guild_store.add_user(g, 1, note="me")

    summary = guild_store.get_guild_summary(g)
    assert summary["configured"] is True
    assert summary["api_base"] == "https://a"
    assert summary["channels"] == {"deploy": 1}
    assert summary["roles"] == [1]
    assert len(summary["users"]) == 1


def test_conn_closes_on_exception(temp_db):
    """
    _conn() 컨텍스트 매니저가 예외 발생 시에도 connection 을 닫는지 검증.

    SQLite 는 fd 누수 시 같은 DB 에 빠른 재연결을 막진 않지만, 의도된
    rollback 동작을 확인한다.
    """
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        with guild_store._conn() as c:
            c.execute("SELECT * FROM nonexistent_table")

    # 다음 연결이 정상 동작해야 함 (락 해제 확인)
    guild_store.set_api(1, "https://ok", "t")
    assert guild_store.get_api(1) == ("https://ok", "t")
