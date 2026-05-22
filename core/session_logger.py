"""
Session Logger (설계서 v6.4 §20.9)
SQLite로 세션 메타데이터 관리 + JSONL로 이벤트 로그 저장.
저장 위치: ~/.recoder/sessions/{session_id}/
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from schemas import SessionRecord, SessionEvent, LLMCallRecord, LLMUsageSummary

RECODER_HOME = Path.home() / ".recoder"
SESSIONS_DIR = RECODER_HOME / "sessions"
DB_PATH = RECODER_HOME / "sessions.db"


class SessionLogger:
    def __init__(self):
        self._init_db()

    def _init_db(self) -> None:
        """SQLite 테이블 초기화 (WAL 저널 모드 활성화)"""
        RECODER_HOME.mkdir(parents=True, exist_ok=True)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # WAL 모드: 동시 읽기/쓰기 성능 향상 (§S-6)
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")

        # Sessions 테이블
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # LLM 호출 기록 테이블
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS llm_calls (
                call_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                operation TEXT NOT NULL,
                provider TEXT NOT NULL,
                model_identifier TEXT NOT NULL,
                region TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                estimated_cost_usd REAL DEFAULT 0.0,
                latency_ms INTEGER DEFAULT 0,
                status TEXT DEFAULT 'success',
                fallback_used BOOLEAN DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                error_type TEXT,
                call_time TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)

        # 인덱스 생성
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_time ON llm_calls(call_time)")

        conn.commit()
        conn.close()

    def create_session(self, project_id: str) -> SessionRecord:
        """새 세션 생성 + DB + 디렉터리 초기화"""
        session_id = str(uuid.uuid4())
        start_time = datetime.now(timezone.utc).isoformat()

        # DB에 저장
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO sessions (session_id, project_id, start_time) VALUES (?, ?, ?)",
            (session_id, project_id, start_time)
        )
        conn.commit()
        conn.close()

        # 세션 디렉터리 생성
        session_dir = SESSIONS_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # SessionRecord 생성
        return SessionRecord(
            session_id=session_id,
            project_id=project_id,
            start_time=start_time
        )

    def end_session(self, session_id: str) -> None:
        """end_time 업데이트"""
        end_time = datetime.now(timezone.utc).isoformat()

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE sessions SET end_time = ? WHERE session_id = ?",
            (end_time, session_id)
        )
        conn.commit()
        conn.close()

    def log_event(self, session_id: str, event: SessionEvent) -> None:
        """~/.recoder/sessions/{session_id}/events.jsonl에 append"""
        session_dir = SESSIONS_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        events_file = session_dir / "events.jsonl"

        # 이벤트를 JSONL 형식으로 저장
        event_dict = {
            "time": event.time,
            "event_type": event.event_type,
            "error_summary": event.error_summary,
            "error_fingerprint": event.error_fingerprint,
            "related_file_names": event.related_file_names,
            "ai_suggestion_summary": event.ai_suggestion_summary,
            "user_action": event.user_action,
            "result": event.result,
            "validation": event.validation,
        }

        with open(events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event_dict) + "\n")

    def log_llm_call(self, session_id: str, record: LLMCallRecord) -> None:
        """~/.recoder/sessions/{session_id}/llm_calls.jsonl에 append + usage 집계"""
        session_dir = SESSIONS_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # DB에 저장
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # 고아 방지: session_id 가 sessions 테이블에 없으면 자동 등록 (§S-6)
        cursor.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,))
        if not cursor.fetchone():
            now = datetime.now(timezone.utc).isoformat()
            cursor.execute(
                "INSERT INTO sessions (session_id, project_id, start_time) VALUES (?, ?, ?)",
                (session_id, "unknown", now),
            )
            conn.commit()
        call_time = datetime.now(timezone.utc).isoformat()

        cursor.execute("""
            INSERT INTO llm_calls (
                call_id, session_id, agent, operation, provider, model_identifier,
                region, input_tokens, output_tokens, total_tokens, estimated_cost_usd,
                latency_ms, status, fallback_used, retry_count, error_type, call_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record.call_id, session_id, record.agent, record.operation,
            record.provider, record.model_identifier, record.region,
            record.input_tokens, record.output_tokens, record.total_tokens,
            record.estimated_cost_usd, record.latency_ms, record.status,
            record.fallback_used, record.retry_count, record.error_type, call_time
        ))
        conn.commit()
        conn.close()

        # JSONL에도 저장
        llm_calls_file = session_dir / "llm_calls.jsonl"
        record_dict = record.to_dict()
        record_dict["call_time"] = call_time

        with open(llm_calls_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record_dict) + "\n")

    def get_session(self, session_id: str) -> Optional[SessionRecord]:
        """DB에서 세션 조회"""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            "SELECT session_id, project_id, start_time, end_time FROM sessions WHERE session_id = ?",
            (session_id,)
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        return SessionRecord(
            session_id=row["session_id"],
            project_id=row["project_id"],
            start_time=row["start_time"],
            end_time=row["end_time"]
        )

    def get_daily_cost(self) -> float:
        """오늘 날짜 LLM 호출 비용 합산"""
        today = datetime.now(timezone.utc).date().isoformat()

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT SUM(estimated_cost_usd) as total
            FROM llm_calls
            WHERE DATE(call_time) = ?
        """, (today,))

        result = cursor.fetchone()
        conn.close()

        return result[0] if result[0] is not None else 0.0

    def get_monthly_cost(self) -> float:
        """이번 달 LLM 호출 비용 합산"""
        today = datetime.now(timezone.utc).date()
        first_day = today.replace(day=1).isoformat()

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT SUM(estimated_cost_usd) as total
            FROM llm_calls
            WHERE DATE(call_time) >= ?
        """, (first_day,))

        result = cursor.fetchone()
        conn.close()

        return result[0] if result[0] is not None else 0.0

    def cleanup_old_sessions(self, days: int = 30) -> int:
        """30일 이상 된 세션 정리. JSONL 디렉토리 + DB 레코드 모두 삭제 후 건수 반환. (§S-6)"""
        import shutil

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT session_id FROM sessions
            WHERE start_time < datetime('now', ? || ' days')
        """, (f"-{days}",))

        old_sessions = [row[0] for row in cursor.fetchall()]

        deleted_count = 0
        for session_id in old_sessions:
            try:
                # JSONL 디렉토리 완전 삭제 (events.jsonl, llm_calls.jsonl 포함)
                session_dir = SESSIONS_DIR / session_id
                if session_dir.exists():
                    shutil.rmtree(session_dir, ignore_errors=True)

                # DB: llm_calls 먼저 (FK), 그 다음 sessions
                cursor.execute("DELETE FROM llm_calls WHERE session_id = ?", (session_id,))
                cursor.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
                deleted_count += 1
            except Exception:
                pass

        conn.commit()
        conn.close()

        return deleted_count

    def cleanup_orphan_dirs(self) -> int:
        """DB에 없는 JSONL 디렉토리 고아 정리. 삭제 건수 반환. (§S-6)"""
        import shutil

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT session_id FROM sessions")
        known_ids = {row[0] for row in cursor.fetchall()}
        conn.close()

        deleted_count = 0
        if SESSIONS_DIR.exists():
            for session_dir in SESSIONS_DIR.iterdir():
                if session_dir.is_dir() and session_dir.name not in known_ids:
                    try:
                        shutil.rmtree(session_dir, ignore_errors=True)
                        deleted_count += 1
                    except Exception:
                        pass

        return deleted_count


_logger_instance: Optional[SessionLogger] = None


def get_session_logger() -> SessionLogger:
    """싱글톤 인스턴스 반환"""
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = SessionLogger()
    return _logger_instance
