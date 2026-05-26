"""
discord-bot/guild_store.py — 서버별(Guild) 설정 저장소

개발자가 운영하는 단일 봇이 여러 Discord 서버를 지원하기 위해
각 서버의 ReCoder API 설정을 로컬 SQLite에 저장한다.

저장 항목:
  - ReCoder API 엔드포인트 & 인증 토큰 (서버별)
  - 알림 채널 ID (deploy / incident / standup)
  - 봇 사용 허용 user_id 목록 (§6.1.4 — 1차 게이트)
  - 봇 사용 허용 역할 ID 목록 (운영 편의용 보조 게이트)
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DB_PATH = Path(__file__).parent / "guild_config.db"
_lock = threading.Lock()


# ── 초기화 ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    """봇 시작 시 1회 호출해 테이블을 생성한다."""
    with _lock, _get_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id    INTEGER PRIMARY KEY,
            api_base    TEXT    NOT NULL DEFAULT '',
            api_token   TEXT    NOT NULL DEFAULT '',
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS guild_channels (
            guild_id     INTEGER NOT NULL,
            channel_type TEXT    NOT NULL,
            channel_id   INTEGER NOT NULL,
            PRIMARY KEY (guild_id, channel_type)
        );

        CREATE TABLE IF NOT EXISTS guild_roles (
            guild_id  INTEGER NOT NULL,
            role_id   INTEGER NOT NULL,
            PRIMARY KEY (guild_id, role_id)
        );

        -- §6.1.4 Discord user_id 화이트리스트 — 1차 권한 게이트.
        -- 클라우드 릴레이(§6.4.3)가 DynamoDB 에 저장하는 'Discord user_id
        -- 매핑' 도 이 테이블을 기준으로 동기화한다.
        CREATE TABLE IF NOT EXISTS guild_users (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            added_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            note       TEXT    NOT NULL DEFAULT '',
            PRIMARY KEY (guild_id, user_id)
        );
        """)


def _get_conn() -> sqlite3.Connection:
    return sqlite3.connect(str(DB_PATH))


# ── API 설정 ────────────────────────────────────────────────────────────────

def set_api(guild_id: int, api_base: str, api_token: str) -> None:
    """서버의 ReCoder API URL 및 토큰을 저장(Upsert)한다."""
    with _lock, _get_conn() as c:
        c.execute(
            """
            INSERT INTO guild_config (guild_id, api_base, api_token, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(guild_id) DO UPDATE SET
                api_base   = excluded.api_base,
                api_token  = excluded.api_token,
                updated_at = excluded.updated_at
            """,
            (guild_id, api_base.rstrip("/"), api_token),
        )


def get_api(guild_id: int) -> Optional[Tuple[str, str]]:
    """(api_base, api_token) 반환. 미설정이면 None."""
    with _get_conn() as c:
        row = c.execute(
            "SELECT api_base, api_token FROM guild_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    if row and row[0]:
        return row[0], row[1]
    return None


# ── 채널 설정 ───────────────────────────────────────────────────────────────

CHANNEL_TYPES = ("deploy", "incident", "standup")


def set_channel(guild_id: int, channel_type: str, channel_id: int) -> None:
    """알림 채널을 설정한다. channel_type: 'deploy' | 'incident' | 'standup'"""
    with _lock, _get_conn() as c:
        c.execute(
            """
            INSERT INTO guild_channels (guild_id, channel_type, channel_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, channel_type) DO UPDATE SET channel_id = excluded.channel_id
            """,
            (guild_id, channel_type, channel_id),
        )


def get_channel(guild_id: int, channel_type: str) -> Optional[int]:
    """알림 채널 ID 반환. 미설정이면 None."""
    with _get_conn() as c:
        row = c.execute(
            "SELECT channel_id FROM guild_channels WHERE guild_id = ? AND channel_type = ?",
            (guild_id, channel_type),
        ).fetchone()
    return row[0] if row else None


def get_all_channels(guild_id: int) -> Dict[str, int]:
    """서버의 모든 채널 설정 {channel_type: channel_id} 반환."""
    with _get_conn() as c:
        rows = c.execute(
            "SELECT channel_type, channel_id FROM guild_channels WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
    return {ctype: cid for ctype, cid in rows}


# ── 역할 설정 ───────────────────────────────────────────────────────────────

def add_role(guild_id: int, role_id: int) -> None:
    """봇 사용 허용 역할을 추가한다."""
    with _lock, _get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO guild_roles (guild_id, role_id) VALUES (?, ?)",
            (guild_id, role_id),
        )


def remove_role(guild_id: int, role_id: int) -> None:
    """봇 사용 허용 역할을 제거한다."""
    with _lock, _get_conn() as c:
        c.execute(
            "DELETE FROM guild_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )


def get_roles(guild_id: int) -> List[int]:
    """서버의 허용 역할 ID 목록 반환."""
    with _get_conn() as c:
        rows = c.execute(
            "SELECT role_id FROM guild_roles WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
    return [r[0] for r in rows]


# ── §6.1.4 user_id 화이트리스트 (1차 게이트) ───────────────────────────────

def add_user(guild_id: int, user_id: int, note: str = "") -> None:
    """봇 사용 허용 user_id 를 추가한다 — §6.1.4 1차 게이트."""
    with _lock, _get_conn() as c:
        c.execute(
            """
            INSERT INTO guild_users (guild_id, user_id, note)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET note = excluded.note
            """,
            (guild_id, user_id, note),
        )


def remove_user(guild_id: int, user_id: int) -> None:
    """봇 사용 허용 user_id 를 제거한다."""
    with _lock, _get_conn() as c:
        c.execute(
            "DELETE FROM guild_users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )


def list_users(guild_id: int) -> List[Tuple[int, str, str]]:
    """서버의 허용 user_id 목록 [(user_id, note, added_at), ...] 반환."""
    with _get_conn() as c:
        rows = c.execute(
            "SELECT user_id, note, added_at FROM guild_users WHERE guild_id = ? ORDER BY added_at",
            (guild_id,),
        ).fetchall()
    return [(r[0], r[1] or "", r[2] or "") for r in rows]


def is_user_allowed(guild_id: int, user_id: int) -> bool:
    """user_id 가 이 서버의 화이트리스트에 있는지 확인한다 — §6.1.4."""
    with _get_conn() as c:
        row = c.execute(
            "SELECT 1 FROM guild_users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return row is not None


def get_user_whitelist_count(guild_id: int) -> int:
    """화이트리스트 등록된 user_id 개수 반환."""
    with _get_conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM guild_users WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return int(row[0]) if row else 0


# ── 서버 전체 삭제 ──────────────────────────────────────────────────────────

def delete_guild(guild_id: int) -> None:
    """봇이 서버에서 추방될 때 해당 서버의 모든 설정을 삭제한다."""
    with _lock, _get_conn() as c:
        c.execute("DELETE FROM guild_config WHERE guild_id = ?", (guild_id,))
        c.execute("DELETE FROM guild_channels WHERE guild_id = ?", (guild_id,))
        c.execute("DELETE FROM guild_roles WHERE guild_id = ?", (guild_id,))
        c.execute("DELETE FROM guild_users WHERE guild_id = ?", (guild_id,))


# ── 요약 조회 ───────────────────────────────────────────────────────────────

def get_guild_summary(guild_id: int) -> dict:
    """설정 현황 요약 반환 (setup status 커맨드용)."""
    api_cfg = get_api(guild_id)
    return {
        "configured": api_cfg is not None,
        "api_base": api_cfg[0] if api_cfg else None,
        "channels": get_all_channels(guild_id),
        "roles": get_roles(guild_id),
        "users": list_users(guild_id),  # §6.1.4 화이트리스트 현황
    }
