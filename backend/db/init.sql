-- AI 업무 어시스턴트 - SQLite 스키마
-- ⚠ 로컬 개발용 (SQLite). EC2 배포 시 PostgreSQL + pgvector 버전으로 교체.

CREATE TABLE IF NOT EXISTS users (
    user_id   TEXT PRIMARY KEY,
    email     TEXT UNIQUE NOT NULL,
    password  TEXT NOT NULL,
    name      TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT REFERENCES users(user_id),
    start_time      TEXT,
    end_time        TEXT,
    ai_summary      TEXT,
    current_task    TEXT,
    has_error       INTEGER DEFAULT 0,
    importance      INTEGER DEFAULT 0,
    resolved        INTEGER DEFAULT 0,
    shared          INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);
-- ⚠ pgvector(embedding 컬럼)는 SQLite 미지원 → RAG는 텍스트 검색으로 대체
