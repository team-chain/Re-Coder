from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from routers.auth import get_current_user
from services.rag import search

router = APIRouter()


@router.get('')
async def rag_search(
    request: Request,
    q: str = Query(..., min_length=1),
    current_user: dict = Depends(get_current_user),
) -> dict:
    results = await search(q, current_user['user_id'], request.app.state.pool)
    return {'results': results}
