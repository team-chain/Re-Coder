# routers/sessions.py
# 세션 저장 / 조회
# ⚠ 로컬 개발용 SQLite 버전 (pgvector 임베딩 없음 → EC2 배포 시 추가)

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from database import get_conn
from routers.auth import get_current_user

router = APIRouter()


# ── 요청 모델 ─────────────────────────────────────────────────
class SessionPayload(BaseModel):
    session_id: str
    start_time: str | None = None
    end_time: str | None = None
    ai_summary: str = ''
    importance_score: int = 0
    error_count: int = 0
    resolved: bool = False
    current_task: str = ''
    shared: bool = False


# ── 세션 저장 ─────────────────────────────────────────────────
@router.post('')
def save_session(data: SessionPayload, user=Depends(get_current_user)):
    user_id = user['user_id']

    with get_conn() as conn:
        conn.execute('''
            INSERT INTO sessions
                (session_id, user_id, start_time, end_time, ai_summary,
                 current_task, has_error, importance, resolved, shared)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                ai_summary   = excluded.ai_summary,
                importance   = excluded.importance,
                resolved     = excluded.resolved,
                end_time     = excluded.end_time,
                current_task = excluded.current_task
        ''', (
            data.session_id, user_id, data.start_time, data.end_time,
            data.ai_summary, data.current_task,
            1 if data.error_count > 0 else 0,
            data.importance_score,
            1 if data.resolved else 0,
            1 if data.shared else 0,
        ))

    return {'session_id': data.session_id, 'status': 'ok'}


# ── 세션 목록 조회 ────────────────────────────────────────────
@router.get('')
def get_sessions(date: str = None, user=Depends(get_current_user)):
    user_id = user['user_id']

    with get_conn() as conn:
        if date:
            rows = conn.execute(
                'SELECT * FROM sessions WHERE user_id = ? AND DATE(start_time) = ? '
                'ORDER BY start_time DESC',
                (user_id, date)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM sessions WHERE user_id = ? '
                'ORDER BY start_time DESC LIMIT 20',
                (user_id,)
            ).fetchall()

    return [dict(r) for r in rows]


# ── 세션 상세 조회 ────────────────────────────────────────────
@router.get('/{session_id}')
def get_session(session_id: str, user=Depends(get_current_user)):
    user_id = user['user_id']

    with get_conn() as conn:
        row = conn.execute(
            'SELECT * FROM sessions WHERE session_id = ? AND user_id = ?',
            (session_id, user_id)
        ).fetchone()

    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, '세션을 찾을 수 없습니다.')

    return dict(row)
