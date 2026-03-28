# database.py
# SQLite 연결 및 초기화
# ⚠ 로컬 개발용. EC2 배포 시 asyncpg + PostgreSQL로 교체.

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / 'ai_assistant.db'


def get_conn() -> sqlite3.Connection:
    """SQLite 연결 반환 (Row를 dict처럼 접근 가능하게 설정)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """DB 초기화 — 테이블이 없으면 생성."""
    sql = (Path(__file__).parent / 'db' / 'init.sql').read_text(encoding='utf-8')
    with get_conn() as conn:
        # SQLite는 여러 문장을 executescript로 실행
        conn.executescript(sql)
    print(f'✅ DB 초기화 완료: {DB_PATH}')
