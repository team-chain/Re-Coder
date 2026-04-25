from __future__ import annotations

from datetime import date, datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from routers.auth import get_current_user
from services.rag import store_embedding

router = APIRouter()


class SessionUpsertRequest(BaseModel):
    session_id: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    ai_summary: str | None = None
    importance_score: int = 0
    error_count: int = 0
    resolved: bool = False
    current_task: str | None = None
    shared: bool = False


def _to_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)
    return None


@router.post('')
async def upsert_session(
    payload: SessionUpsertRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> dict:
    pool = request.app.state.pool
    user_id = current_user['user_id']
    existing_owner = await pool.fetchval(
        'SELECT user_id FROM sessions WHERE session_id=$1',
        payload.session_id,
    )
    if existing_owner and existing_owner != user_id:
        raise HTTPException(status_code=403, detail='Session ID already belongs to another user')

    start_time = _to_datetime(payload.start_time)
    end_time = _to_datetime(payload.end_time)
    has_error = payload.error_count > 0

    should_store_embedding = False
    if payload.ai_summary:
        existing_summary = await pool.fetchval(
            'SELECT ai_summary FROM sessions WHERE session_id=$1',
            payload.session_id,
        )
        if existing_summary != payload.ai_summary:
            should_store_embedding = True

    upserted = await pool.fetchrow(
        '''
        INSERT INTO sessions (
            session_id, user_id, start_time, end_time, ai_summary,
            current_task, has_error, importance, resolved, shared
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (session_id) DO UPDATE
        SET start_time=EXCLUDED.start_time,
            end_time=EXCLUDED.end_time,
            ai_summary=EXCLUDED.ai_summary,
            current_task=EXCLUDED.current_task,
            has_error=EXCLUDED.has_error,
            importance=EXCLUDED.importance,
            resolved=EXCLUDED.resolved,
            shared=EXCLUDED.shared
        WHERE sessions.user_id=EXCLUDED.user_id
        RETURNING session_id
        ''',
        payload.session_id,
        user_id,
        start_time,
        end_time,
        payload.ai_summary,
        payload.current_task,
        has_error,
        payload.importance_score,
        payload.resolved,
        payload.shared,
    )
    if upserted is None:
        raise HTTPException(status_code=403, detail='Session ID already belongs to another user')

    if should_store_embedding:
        await store_embedding(payload.session_id, payload.ai_summary, user_id, pool)

    return {'ok': True, 'session_id': payload.session_id}


@router.get('')
async def list_sessions(
    request: Request,
    current_user: dict = Depends(get_current_user),
    query_date: date | None = Query(default=None, alias='date'),
) -> list[dict]:
    pool = request.app.state.pool
    user_id = current_user['user_id']

    if query_date is not None:
        start_dt = datetime.combine(query_date, time.min)
        end_dt = datetime.combine(query_date, time.max)
        rows = await pool.fetch(
            '''
            SELECT session_id, user_id, start_time, end_time, ai_summary,
                   current_task, has_error, importance, resolved, shared, created_at
            FROM sessions
            WHERE user_id=$1 AND created_at BETWEEN $2 AND $3
            ORDER BY created_at DESC
            ''',
            user_id,
            start_dt,
            end_dt,
        )
    else:
        rows = await pool.fetch(
            '''
            SELECT session_id, user_id, start_time, end_time, ai_summary,
                   current_task, has_error, importance, resolved, shared, created_at
            FROM sessions
            WHERE user_id=$1
            ORDER BY created_at DESC
            LIMIT 20
            ''',
            user_id,
        )

    return [dict(row) for row in rows]


@router.get('/{session_id}')
async def get_session_detail(
    session_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> dict:
    row = await request.app.state.pool.fetchrow(
        '''
        SELECT session_id, user_id, start_time, end_time, ai_summary,
               current_task, has_error, importance, resolved, shared, created_at
        FROM sessions
        WHERE session_id=$1 AND user_id=$2
        ''',
        session_id,
        current_user['user_id'],
    )
    if not row:
        raise HTTPException(status_code=404, detail='Session not found')
    return dict(row)
