"""공격·장애 감지 알림 API (2.0.25) — 확장이 1분마다 새 알림을 묻고, 승인한 대응만 적용한다."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

try:
    import ops_alerts  # type: ignore
except ImportError:  # pragma: no cover
    from core import ops_alerts  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ops/alerts", tags=["ops-alerts"])


def session_for(region: str):
    import boto3
    return boto3.session.Session(region_name=region)


class AlertList(BaseModel):
    alerts: list[dict]
    monitors: list[dict]


class ActRequest(BaseModel):
    action: Literal["block_ip", "shield_strict", "scale", "rollback"]
    value: Optional[Any] = None
    approved: bool = Field(..., description="사용자가 승인 카드에서 고른 결과")


@router.get("", response_model=AlertList)
async def list_alerts(since: str = "") -> AlertList:
    return AlertList(alerts=ops_alerts.alerts(since), monitors=ops_alerts.monitors())


@router.post("/{alert_id}/act")
async def act(alert_id: str, body: ActRequest) -> dict:
    try:
        return await asyncio.to_thread(ops_alerts.act, alert_id, body.action, body.value,
                                       approved=body.approved, session_factory=session_for)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("대응 적용 실패: %s", exc)
        raise HTTPException(status_code=502, detail=f"대응을 적용하지 못했습니다: {ops_alerts._err(exc)}") from exc


@router.delete("/monitors/{key:path}")
async def stop_monitor(key: str) -> dict:
    return {"removed": ops_alerts.unregister(key)}


@router.post("/check")
async def check_now() -> dict:
    """지금 한 번 감시(확장의 [지금 확인] 버튼·시연용)."""
    raised = await asyncio.to_thread(ops_alerts.Monitor(session_for).tick)
    return {"raised": raised}
