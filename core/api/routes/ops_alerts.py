"""공격·장애 감지 알림 API (2.0.25) — 확장이 1분마다 새 알림을 묻고, 승인한 대응만 적용한다."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
try:
    from api.errors import internal_error as _internal_error
except ImportError:  # pragma: no cover
    from core.api.errors import internal_error as _internal_error  # type: ignore

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
    #: 2.0.32 — 확장이 기억한 마지막 알림을 찾지 못했다(목록이 잘리거나 바뀜). 전체를 돌려주니 '새 알림'으로 다 띄우지 말 것.
    cursor_reset: bool = False


class ActRequest(BaseModel):
    action: Literal["block_ip", "shield_strict", "scale", "rollback", "upgrade"]
    value: Optional[Any] = None
    approved: bool = Field(..., description="사용자가 승인 카드에서 고른 결과")


@router.get("", response_model=AlertList)
async def list_alerts(since: str = "") -> AlertList:
    return AlertList(alerts=ops_alerts.alerts(since), monitors=ops_alerts.monitors(),
                     cursor_reset=not ops_alerts.cursor_known(since))


@router.post("/{alert_id}/act")
async def act(alert_id: str, body: ActRequest) -> dict:
    try:
        return await asyncio.to_thread(ops_alerts.act, alert_id, body.action, body.value,
                                       approved=body.approved, session_factory=session_for)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _internal_error(exc, status_code=502, message="대응을 적용하지 못했습니다. AWS 권한과 연결 상태를 확인해 주십시오.",
                              logger=logger) from exc


@router.delete("/monitors/{key:path}")
async def stop_monitor(key: str) -> dict:
    return {"removed": ops_alerts.unregister(key)}


@router.post("/check")
async def check_now() -> dict:
    """지금 한 번 감시(확장의 [지금 확인] 버튼·시연용)."""
    raised = await asyncio.to_thread(ops_alerts.Monitor(session_for).tick)
    return {"raised": raised}
