"""작업별 AI 공급자·비용 원장·공급자 비교 API (2.0.36).

GET  /api/models/routing            등급별 공급자(키 값 없음)
GET  /api/models/costs?days=30      등급·공급자·모델별 토큰·비용
POST /api/models/compare            같은 요청을 두 공급자로(백그라운드) — 작업 ID 를 돌려준다
GET  /api/models/compare/latest     마지막 비교
GET  /api/models/compare/{job_id}   진행·결과
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

try:
    import model_compare  # type: ignore
    from llm import cost_ledger  # type: ignore
    from llm.provider_router import get_provider_router  # type: ignore
except ImportError:  # pragma: no cover
    from core import model_compare  # type: ignore
    from core.llm import cost_ledger  # type: ignore
    from core.llm.provider_router import get_provider_router  # type: ignore

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("/routing")
async def routing() -> dict:
    return get_provider_router().routing_status()


@router.post("/check")
async def check_models() -> dict:
    from llm.model_checks import check
    provider_router = get_provider_router()
    await asyncio.to_thread(check, provider_router)
    return provider_router.routing_status()


@router.get("/costs")
async def costs(days: int = Query(30, ge=1, le=365)) -> dict:
    #: 원장 파일 읽기는 디스크 I/O — 이벤트 루프를 막지 않게.
    return await asyncio.to_thread(cost_ledger.summary, days)


class CompareRequest(BaseModel):
    instruction: str = Field("", max_length=model_compare.MAX_INSTRUCTION)
    workspace_path: str = Field("", max_length=1024)
    providers: list[str] = Field(default_factory=list, max_length=2)


@router.post("/compare")
async def compare(body: CompareRequest) -> dict:
    try:
        return await asyncio.to_thread(model_compare.start, body.instruction, body.workspace_path, body.providers)
    except model_compare.CompareBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except model_compare.CompareError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/compare/latest")
async def compare_latest() -> dict:
    return {"job": model_compare.latest()}


@router.get("/compare/{job_id}")
async def compare_status(job_id: str) -> dict:
    job = model_compare.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="비교 작업을 찾을 수 없습니다.")
    return job
