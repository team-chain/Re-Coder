"""
core/api/routes/relay.py — Hybrid Cloud Relay 큐 REST API (설계서 §6.4.2 흐름 1)

엔드포인트:
  POST /api/relay/queue/dequeue              — Local Core 의 polling 진입점
  POST /api/relay/queue/enqueue              — Discord bot / 외부 → 명령 push
  POST /api/relay/queue/{command_id}/complete — 처리 결과 보고
  GET  /api/relay/queue/history              — user 이력 조회
  GET  /api/relay/status                     — DynamoDB 연결 / 테이블 상태
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/relay", tags=["relay"])


# ── Pydantic 모델 ─────────────────────────────────────────────────────

class EnqueueRequest(BaseModel):
    user_id: str
    command_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    source: str = "discord"


class DequeueRequest(BaseModel):
    user_id: str
    limit: int = 10


class CompleteRequest(BaseModel):
    user_id: str
    command_id: str = ""
    result: Optional[Dict[str, Any]] = None
    error: str = ""
    success: bool = True


# ── 큐 인스턴스 헬퍼 (lazy) ───────────────────────────────────────────

def _get_queue():
    """DynamoCommandQueue 를 lazy 로 만든다. 실패 시 503."""
    try:
        from relay.dynamo_queue import DynamoCommandQueue
        return DynamoCommandQueue()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"DynamoDB relay unavailable: {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"relay init error: {exc}",
        ) from exc


# ── 엔드포인트 ────────────────────────────────────────────────────────

@router.post("/queue/enqueue")
async def enqueue(body: EnqueueRequest) -> Dict[str, Any]:
    """Discord bot 또는 외부 서비스가 명령을 큐에 push."""
    q = _get_queue()
    result = q.enqueue(
        user_id=body.user_id,
        command_type=body.command_type,
        payload=body.payload,
        source=body.source,
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=500, detail=result.get("error", "enqueue failed"))
    return result


@router.post("/queue/dequeue")
async def dequeue(body: DequeueRequest) -> Dict[str, Any]:
    """Local Core 가 polling 시 호출 — pending → processing 전환."""
    q = _get_queue()
    result = q.dequeue_pending(user_id=body.user_id, limit=body.limit)
    return result


@router.post("/queue/{command_id}/complete")
async def complete(command_id: str, body: CompleteRequest) -> Dict[str, Any]:
    """처리 결과 보고. body.command_id 가 path 와 다르면 path 우선."""
    q = _get_queue()
    cid = command_id or body.command_id
    if not cid:
        raise HTTPException(status_code=400, detail="command_id is required")

    if body.success:
        result = q.mark_done(
            user_id=body.user_id,
            command_id=cid,
            result=body.result or {},
        )
    else:
        result = q.mark_failed(
            user_id=body.user_id,
            command_id=cid,
            error=body.error or "unknown",
        )
    if result.get("status") != "ok":
        # 동시성 충돌 등은 409 로 노출
        raise HTTPException(status_code=409, detail=result.get("error", "complete failed"))
    return result


@router.get("/queue/history")
async def history(
    user_id: str = Query(..., description="Discord user_id"),
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """user_id 의 최근 명령 이력."""
    q = _get_queue()
    return q.list_for_user(user_id=user_id, limit=limit)


@router.get("/status")
async def status() -> Dict[str, Any]:
    """
    DynamoDB 연결 / 테이블 존재 상태 확인.
    자격증명이 없어도 본 엔드포인트는 200 으로 답하되 connected=false.
    """
    info: Dict[str, Any] = {
        "enabled":          os.getenv("RECODER_RELAY_ENABLED", "false").lower() == "true",
        "table_name":       os.getenv("RECODER_RELAY_QUEUE_TABLE", "recoder-command-queue"),
        "configured_users": _configured_user_ids(),
        "connected":        False,
        "table":            None,
    }
    try:
        from relay.dynamo_queue import DynamoCommandQueue
        q = DynamoCommandQueue()
        info["connected"] = True
        info["table"] = q.table_status()
    except RuntimeError as exc:
        info["error"] = str(exc)
    except Exception as exc:
        info["error"] = f"init failed: {exc}"

    # poller 상태도 노출 (서버에 인스턴스가 보관돼 있으면)
    try:
        from api.routes import relay as _self  # noqa: WPS437
        poller = getattr(_self, "_active_poller", None)
        if poller is not None:
            info["poller_running"] = poller.is_running()
            info["poller_user_ids"] = poller.user_ids
            info["poller_interval"] = poller.interval
    except Exception:
        pass

    return info


def _configured_user_ids() -> List[str]:
    raw = os.getenv("RECODER_RELAY_USER_IDS", "").strip()
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    single = os.getenv("RECODER_RELAY_USER_ID", "").strip()
    return [single] if single else []


# 백그라운드 poller 인스턴스 보관 (main.py 의 lifespan 이 set)
_active_poller = None  # type: ignore
