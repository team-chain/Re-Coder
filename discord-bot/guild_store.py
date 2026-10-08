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
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

DB_PATH = Path(__file__).parent / "guild_config.db"
_lock = threading.Lock()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """
    sqlite3.Connection 을 컨텍스트로 감싸 항상 close 보장.

    내장 `with sqlite3.connect(...) as c:` 는 트랜잭션 commit/rollback 만
    처리할 뿐 connection 자체는 닫지 않는다 — 장기 실행 봇에서 fd 누수의
    원인이 된다. 본 헬퍼는 try/finally 로 close 까지 확실히 처리한다.
    """
    c = sqlite3.connect(str(DB_PATH))
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


# ── 초기화 ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    """봇 시작 시 1회 호출해 테이블을 생성한다."""
    with _lock, _conn() as c:
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

        -- Phase 2 per-user 라우팅: Discord user_id → student_id 1:1 바인딩.
        -- /recoder link 로 학생이 본인 계정과 student_id 를 연결한다.
        -- 디스코드 명령이 본인 VSCode(해당 student_id 로 브리지에 붙은 연결)로만
        -- 라우팅되도록 하는 기준.
        CREATE TABLE IF NOT EXISTS user_bindings (
            discord_user_id INTEGER PRIMARY KEY,
            student_id      TEXT    NOT NULL,
            token_sha256    TEXT    NOT NULL DEFAULT '',
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        """)
        # 구 스키마 마이그레이션 — token_sha256 컬럼이 없으면 추가.
        try:
            c.execute("ALTER TABLE user_bindings ADD COLUMN token_sha256 TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass


def _get_conn() -> sqlite3.Connection:
    """
    레거시: 새 코드는 `_conn()` 컨텍스트 매니저를 사용하세요.

    이 함수는 close 책임을 호출자에게 떠넘기므로 try/finally 가 필수.
    호환성을 위해서만 유지합니다.
    """
    return sqlite3.connect(str(DB_PATH))


# ── API 설정 ────────────────────────────────────────────────────────────────

def api_base_problem(api_base: str) -> Optional[str]:
    """저장하면 안 되는 API 주소면 그 이유, 괜찮으면 None.

    봇은 이 주소로 세션 토큰을 붙여 요청을 보낸다 — 클라우드 메타데이터 주소(169.254.169.254 등)나
    http(s) 가 아닌 주소를 받으면 봇이 도는 서버 내부를 찌르는 통로가 된다(SSRF).
    같은 PC·사내망에서 직접 운영하는 봇이 localhost·사설 IP 의 Core 를 쓰는 것은 그대로 허용한다
    (공개 호스팅이면 RECODER_BLOCK_PRIVATE_API_BASE=1 로 사설·루프백 주소도 막는다).
    """
    import ipaddress
    import os
    import socket
    from urllib.parse import urlparse

    parsed = urlparse((api_base or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "API 주소는 http:// 또는 https:// 로 시작하는 주소여야 합니다."
    host = parsed.hostname.lower().rstrip(".")
    if host in ("metadata.google.internal", "metadata", "instance-data", "instance-data.ec2.internal"):
        return "클라우드 메타데이터 주소는 API 주소로 쓸 수 없습니다."
    block_private = os.getenv("RECODER_BLOCK_PRIVATE_API_BASE", "0") == "1"
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
        addresses = {info[4][0] for info in infos}
    except (OSError, UnicodeError, ValueError):
        addresses = set()
        try:
            addresses = {str(ipaddress.ip_address(host))}
        except ValueError:
            pass
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            continue
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            return "링크 로컬·메타데이터 대역(169.254.x.x 등) 주소는 API 주소로 쓸 수 없습니다."
        if block_private and (ip.is_private or ip.is_loopback):
            return "이 봇은 공개 호스팅 모드라 사설·루프백 주소를 API 주소로 쓸 수 없습니다."
    return None


def set_api(guild_id: int, api_base: str, api_token: str) -> None:
    """서버의 ReCoder API URL 및 토큰을 저장(Upsert)한다."""
    problem = api_base_problem(api_base)
    if problem:
        raise ValueError(problem)
    with _lock, _conn() as c:
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
    with _conn() as c:
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
    with _lock, _conn() as c:
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
    with _conn() as c:
        row = c.execute(
            "SELECT channel_id FROM guild_channels WHERE guild_id = ? AND channel_type = ?",
            (guild_id, channel_type),
        ).fetchone()
    return row[0] if row else None


def get_all_channels(guild_id: int) -> Dict[str, int]:
    """서버의 모든 채널 설정 {channel_type: channel_id} 반환."""
    with _conn() as c:
        rows = c.execute(
            "SELECT channel_type, channel_id FROM guild_channels WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
    return {ctype: cid for ctype, cid in rows}


# ── 역할 설정 ───────────────────────────────────────────────────────────────

def add_role(guild_id: int, role_id: int) -> None:
    """봇 사용 허용 역할을 추가한다."""
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO guild_roles (guild_id, role_id) VALUES (?, ?)",
            (guild_id, role_id),
        )


def remove_role(guild_id: int, role_id: int) -> None:
    """봇 사용 허용 역할을 제거한다."""
    with _lock, _conn() as c:
        c.execute(
            "DELETE FROM guild_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )


def get_roles(guild_id: int) -> List[int]:
    """서버의 허용 역할 ID 목록 반환."""
    with _conn() as c:
        rows = c.execute(
            "SELECT role_id FROM guild_roles WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
    return [r[0] for r in rows]


# ── §6.1.4 user_id 화이트리스트 (1차 게이트) ───────────────────────────────

def add_user(guild_id: int, user_id: int, note: str = "") -> None:
    """봇 사용 허용 user_id 를 추가한다 — §6.1.4 1차 게이트."""
    with _lock, _conn() as c:
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
    with _lock, _conn() as c:
        c.execute(
            "DELETE FROM guild_users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )


def list_users(guild_id: int) -> List[Tuple[int, str, str]]:
    """서버의 허용 user_id 목록 [(user_id, note, added_at), ...] 반환."""
    with _conn() as c:
        rows = c.execute(
            "SELECT user_id, note, added_at FROM guild_users WHERE guild_id = ? ORDER BY added_at",
            (guild_id,),
        ).fetchall()
    return [(r[0], r[1] or "", r[2] or "") for r in rows]


def is_user_allowed(guild_id: int, user_id: int) -> bool:
    """user_id 가 이 서버의 화이트리스트에 있는지 확인한다 — §6.1.4."""
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM guild_users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return row is not None


def get_user_whitelist_count(guild_id: int) -> int:
    """화이트리스트 등록된 user_id 개수 반환."""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM guild_users WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return int(row[0]) if row else 0


# ── 서버 전체 삭제 ──────────────────────────────────────────────────────────

def delete_guild(guild_id: int) -> None:
    """봇이 서버에서 추방될 때 해당 서버의 모든 설정을 삭제한다."""
    with _lock, _conn() as c:
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


# ── Phase 2: Discord user ↔ student_id 바인딩 ────────────────────────────────

import hashlib as _hashlib


def _token_hash(raw_token: str) -> str:
    return _hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def set_binding(discord_user_id: int, student_id: str, token_sha256: str = "") -> None:
    """학생 본인 Discord 계정 ↔ student_id 연결(덮어쓰기).

    token_sha256 은 **소유 증명**이다 — 브리지가 이 해시로 "이 student_id 스트림을
    받을 자격" 을 검증한다. 빈 값이면 소유 증명 없는 레거시 바인딩이며, 브리지는
    RECODER_BRIDGE_REQUIRE_SECRET=1 일 때 그런 바인딩의 수신을 거부한다.
    """
    with _lock, _conn() as c:
        c.execute(
            """
            INSERT INTO user_bindings (discord_user_id, student_id, token_sha256, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(discord_user_id)
            DO UPDATE SET student_id = excluded.student_id,
                          token_sha256 = excluded.token_sha256,
                          updated_at = datetime('now')
            """,
            (int(discord_user_id), str(student_id), str(token_sha256)),
        )


def verify_student_secret(student_id: str, raw_token: str) -> bool:
    """raw_token(rcdr_<sid>_<secret> 전체)이 이 student_id 의 등록 해시와 일치하는가.

    등록 해시가 없으면(레거시 바인딩) False — 소유를 증명할 수 없으므로.
    """
    if not student_id or not raw_token:
        return False
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT token_sha256 FROM user_bindings WHERE student_id = ? AND token_sha256 != ''",
            (str(student_id),),
        ).fetchone()
    if not row:
        return False
    import hmac as _hmac
    return _hmac.compare_digest(row[0], _token_hash(raw_token))


def get_student_id(discord_user_id: int) -> Optional[str]:
    """Discord user_id 의 student_id 반환(없으면 None)."""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT student_id FROM user_bindings WHERE discord_user_id = ?",
            (int(discord_user_id),),
        ).fetchone()
    return row[0] if row else None


def remove_binding(discord_user_id: int) -> None:
    with _lock, _conn() as c:
        c.execute(
            "DELETE FROM user_bindings WHERE discord_user_id = ?",
            (int(discord_user_id),),
        )
