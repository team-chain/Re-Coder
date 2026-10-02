"""Explicit document-grounded repair API, protected by the core session middleware."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from grounded_repair.knowledge import KnowledgeIndex
from grounded_repair.pipeline import RepairPipeline
from grounded_repair.store import RepairStore

router = APIRouter(prefix="/api/repair", tags=["repair"])
_lock = asyncio.Lock()


def _store():
    from persistence.db import get_default_db_path
    default = get_default_db_path().with_name("grounded_repair.db")
    return RepairStore(Path(os.getenv("RECODER_REPAIR_DB", str(default))))


def _pipeline():
    from llm.provider_router import get_provider_router
    source = os.getenv("RECODER_REPAIR_CORPUS")
    return RepairPipeline(get_provider_router(), KnowledgeIndex.load(Path(source) if source else None), _store())


class PrepareRequest(BaseModel):
    workspace_path: str = Field(min_length=1, max_length=4096)
    log: str = Field(min_length=1, max_length=1_000_000)
    stage: Literal["build", "run", "ecs", "iam", "s3"] = "build"
    strategy: Literal["A", "B", "C", "D"] = "D"
    use_cache: bool = True


def public_result(result):
    # Hashes and full replacement files stay server-side until explicit approval.
    return {k: v for k, v in result.items() if k not in {"manifest", "edits"}}


@router.post("/prepare")
async def prepare(request: PrepareRequest):
    if _lock.locked():
        raise HTTPException(409, "A repair is already in progress")
    async with _lock:
        try:
            pipeline = await asyncio.to_thread(_pipeline)
            result = await pipeline.run(request.workspace_path, request.log, request.stage,
                                        request.strategy, request.use_cache and request.strategy == "D")
            return public_result(result)
        except (ValueError, OSError) as exc:
            from context_gate import mask_secrets
            raise HTTPException(422, mask_secrets(str(exc))) from exc


@router.get("/{run_id}")
async def get_run(run_id: str):
    result = await asyncio.to_thread(_store().get, run_id)
    if result is None:
        raise HTTPException(404, "Unknown repair run")
    return public_result(result)


@router.post("/{run_id}/approve")
async def approve(run_id: str):
    async with _lock:
        try:
            return public_result(await asyncio.to_thread(_store().approve, run_id))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
