"""AWS 리소스 정리 API (2.0.26) — 목록(아무것도 안 바꿈) · 중지(되살릴 수 있음) · 완전 삭제(승인한 항목만)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

try:
    import aws_cleanup  # type: ignore
except ImportError:  # pragma: no cover
    from core import aws_cleanup  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/aws/cleanup", tags=["aws-cleanup"])

_REGION = r"^[a-z]{2}(-gov)?-[a-z]+-\d$"


def clients_for(region: str) -> dict:
    import boto3
    session = boto3.session.Session(region_name=region)
    return {name: session.client(name) for name in aws_cleanup.CLIENTS}


def deploy_records() -> list[dict]:
    """ReCoder 배포 기록의 클러스터·서비스 — 태그가 없는 옛 배포도 "수동 확인 필요" 로 보이게."""
    try:
        try:
            from api.routes.ecs import _store_path  # type: ignore
        except ImportError:  # pragma: no cover
            from core.api.routes.ecs import _store_path  # type: ignore
        path = _store_path()
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        return [{"cluster": r.get("cluster"), "service": r.get("service"), "region": r.get("region")}
                for r in raw if isinstance(r, dict)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("배포 기록을 읽지 못했습니다(정리 목록은 태그로만): %s", exc)
        return []


def _records(region: str) -> list[dict]:
    return [r for r in deploy_records() if (r.get("region") or region) == region]


def _aws_error(exc: Exception) -> HTTPException:
    try:
        from aws_infra import error_message  # type: ignore
    except ImportError:  # pragma: no cover
        from core.aws_infra import error_message  # type: ignore
    return HTTPException(status_code=502, detail=f"AWS 에 연결하지 못했습니다: {error_message(exc)}")


class PlanRequest(BaseModel):
    region: str = Field(..., pattern=_REGION)
    service: str = Field(default="", max_length=255)


class StopRequest(BaseModel):
    region: str = Field(..., pattern=_REGION)
    cluster: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,255}$")
    service: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,255}$")


class DeleteRequest(BaseModel):
    region: str = Field(..., pattern=_REGION)
    ids: list[str] = Field(..., min_length=1, max_length=200)
    approved: bool = Field(..., description="사용자가 승인 카드에서 항목을 확인하고 누른 결과")
    #: 실수 방지 — 승인 카드에 보여 준 개수와 같아야 한다.
    count: Optional[int] = None


@router.post("/plan")
async def plan(body: PlanRequest) -> dict:
    try:
        return await asyncio.to_thread(aws_cleanup.plan, clients_for(body.region), region=body.region,
                                       records=_records(body.region), service=body.service.strip())
    except Exception as exc:  # noqa: BLE001
        raise _aws_error(exc) from exc


@router.post("/stop")
async def stop(body: StopRequest) -> dict:
    try:
        return await asyncio.to_thread(aws_cleanup.stop, clients_for(body.region), region=body.region,
                                       cluster=body.cluster, service=body.service)
    except Exception as exc:  # noqa: BLE001
        raise _aws_error(exc) from exc


@router.post("/delete")
async def delete(body: DeleteRequest) -> dict:
    if not body.approved:
        raise HTTPException(status_code=400, detail="승인 없이 지우지 않습니다.")
    ids = [i for i in dict.fromkeys(body.ids) if re.fullmatch(r"[a-z_0-9]+:[^\s]{1,512}", i)]
    if body.count is not None and body.count != len(ids):
        raise HTTPException(status_code=400, detail="승인한 항목 수와 요청 항목 수가 다릅니다 — 목록을 다시 불러오세요.")
    try:
        return await asyncio.to_thread(aws_cleanup.delete, clients_for(body.region), ids, region=body.region,
                                       approved=True, records=_records(body.region))
    except Exception as exc:  # noqa: BLE001
        raise _aws_error(exc) from exc
