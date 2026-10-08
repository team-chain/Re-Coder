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
    from grounded_repair.config import load_index, repair_router
    return RepairPipeline(repair_router(), load_index(), _store())


class PrepareRequest(BaseModel):
    workspace_path: str = Field(min_length=1, max_length=4096)
    log: str = Field(min_length=1, max_length=1_000_000)
    stage: Literal["build", "run", "ecs", "iam", "s3"] = "build"
    strategy: Literal["A", "B", "C", "D"] = "D"
    use_cache: bool = True


class ApproveRequest(BaseModel):
    workspace_path: str


def public_result(result):
    # Hashes and full replacement files stay server-side until explicit approval.
    public = {k: v for k, v in result.items() if k not in {"manifest", "edits"}}
    if "attempts" in public:
        public["attempts"] = [{k: v for k, v in a.items() if k != "proposal"} for a in public["attempts"]]
    if "suggestion" in public:
        public["suggestion"] = {k: v for k, v in public["suggestion"].items() if k != "edits"}
    return public


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
async def approve(run_id: str, request: ApproveRequest | None = None):
    async with _lock:
        try:
            return public_result(await asyncio.to_thread(_store().approve, run_id, request.workspace_path if request else None))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
