"""모니터 · 24시간 감시자 API (2.0.48).

모니터 화면은 한 번에 한 서비스를 본다(감지 알림에 등록된 ECS 서비스 — ops_alerts.monitors()).
AWS 를 바꾸는 요청(설치 · 삭제 · 롤백)은 모두 approved=true 가 있어야 하고, 등록된 서비스에만 할 수 있다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

try:
    import ops_alerts  # type: ignore
    import watcher  # type: ignore
except ImportError:  # pragma: no cover
    from core import ops_alerts, watcher  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ops/watch", tags=["ops-watch"])
SERIES_POINTS = 48  # 24시간을 30분 칸으로


def session_for(region: str):
    import boto3
    return boto3.session.Session(region_name=region)


def _monitor(key: str) -> dict:
    found = next((m for m in ops_alerts.monitors() if m.get("key") == key), None)
    if found is None:
        raise HTTPException(status_code=404, detail="감시 중인 서비스가 아닙니다 — ECS 배포에서 감지 알림을 켜면 생깁니다.")
    return found


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    #: 403 은 확장이 '세션 토큰 문제'로 보고 한 번 더 보낸다 — 승인 없음 · AWS 권한 없음은 다른 코드로(설치가 두 번 돌지 않게).
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, (watcher.WatchError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    code = watcher._code(exc)
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        return HTTPException(status_code=502, detail=f"AWS 권한이 없습니다({watcher._msg(exc)}) — 권한 템플릿을 최신으로 다시 적용하세요.")
    logger.warning("감시자 요청 실패: %s", exc)
    return HTTPException(status_code=502, detail=f"AWS 요청이 실패했습니다: {watcher._msg(exc)}")


def _downsample(values: list[float], points: int = SERIES_POINTS) -> list[float]:
    """5분 칸 288개 → 30분 칸 48개(합계). 짧으면 있는 만큼."""
    if not values:
        return []
    size = max(1, len(values) // points)
    tail = values[-size * points:] if len(values) >= size * points else values
    return [float(sum(tail[i:i + size])) for i in range(0, len(tail), size)]


def _stable_from_records(m: dict, current_td: str) -> str:
    """로컬 배포 기록에서 '지금 버전 직전에 성공한 버전'을 찾는다(설치 직후부터 롤백 링크를 준비하려고)."""
    try:
        try:
            from api.routes import ecs as ecs_routes  # type: ignore
        except ImportError:  # pragma: no cover
            from core.api.routes import ecs as ecs_routes  # type: ignore
        records = sorted(ecs_routes._deploy_records.values(), key=lambda r: str(r.started_at), reverse=True)
        for r in records:
            if (r.region, r.cluster, r.service) == (m["region"], m["cluster"], m["service"]) \
                    and str(getattr(r.status, "value", r.status)) == "succeeded" \
                    and watcher._rev(str(r.task_definition_arn or "")) == watcher._rev(current_td):
                return str(r.previous_task_definition_arn or "")
    except Exception as exc:  # noqa: BLE001
        logger.info("배포 기록에서 이전 버전을 찾지 못했습니다: %s", exc)
    return ""


def overview(key: str = "", now: Optional[float] = None, *, light: bool = False) -> dict:
    """모니터 화면 한 장 — 서비스 상태 · 24시간 지표 · 감시자 상태 · 기록 · 승인 대기."""
    mons = ops_alerts.monitors()
    listed = [{"key": m["key"], "service": m["service"], "region": m["region"], "cluster": m["cluster"]} for m in mons]
    if not mons:
        return {"monitors": [], "current": None}
    m = next((x for x in mons if x["key"] == key), mons[-1])
    now = now or time.time()
    gaps: list[str] = []
    session = session_for(m["region"])
    cur: dict[str, Any] = {"key": m["key"], "service": m["service"], "region": m["region"], "cluster": m["cluster"],
                           "metrics": bool(m.get("metrics", True))}
    current_td = ""
    try:
        svc = (session.client("ecs").describe_services(cluster=m["cluster"], services=[m["service"]]).get("services") or [{}])[0]
        current_td = str(svc.get("taskDefinition") or "")
        cur["service_state"] = {"status": svc.get("status", "MISSING"), "running": int(svc.get("runningCount") or 0),
                                "desired": int(svc.get("desiredCount") or 0), "revision": watcher._rev(current_td),
                                "task_definition": current_td}
    except Exception as exc:  # noqa: BLE001
        cur["service_state"] = None
        gaps.append(f"서비스 상태를 읽지 못했습니다: {watcher._msg(exc)}")
    if m.get("metrics", True) and not light:
        try:
            series, seen = ops_alerts.read_series(session.client("cloudwatch"), m["namespace"], hours=24)
            req, err, blk = series.get("Requests", []), series.get("Errors", []), series.get("Blocked", [])
            last_req = req[-1] if req else 0.0
            cur["series"] = {"requests": _downsample(req), "errors": _downsample(err), "blocked": _downsample(blk)}
            cur["now"] = {"rpm": round(last_req / 5.0, 1), "error_rate": round((err[-1] / last_req) if last_req and err else 0.0, 4),
                          "points": seen}
        except Exception as exc:  # noqa: BLE001
            cur["series"] = None
            gaps.append(f"지표를 읽지 못했습니다: {watcher._msg(exc)}")
    else:
        cur["series"] = None
    status = watcher.status(session, region=m["region"], cluster=m["cluster"], service=m["service"], now=now)
    cur["watcher"] = status
    evts: list[dict] = []
    if status.get("installed"):
        try:
            evts = watcher.events(session, region=m["region"], cluster=m["cluster"], service=m["service"], limit=100)
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"감시자 기록을 읽지 못했습니다: {watcher._msg(exc)}")
    elif status.get("installed") is None:
        gaps.append(f"감시자 상태를 확인하지 못했습니다: {status.get('error', '')}")
    cur["events"] = evts
    cur["offers"] = watcher.offers(evts, current_td, now=now) if current_td else []
    cur["alerts"] = [a for a in ops_alerts.alerts() if a.get("key") == m["key"]][-50:]
    cur["gaps"] = gaps
    return {"monitors": listed, "current": cur}


class KeyBody(BaseModel):
    key: str = Field(..., min_length=3, max_length=600)


class PlanBody(KeyBody):
    role: str = Field("", max_length=400)


class InstallBody(PlanBody):
    discord: str = Field("", max_length=400)
    approved: bool = False


class ApproveBody(KeyBody):
    approved: bool = False


class SettingsBody(KeyBody):
    discord: Optional[str] = Field(None, max_length=400)
    email: str = Field("", max_length=260)


class RollbackBody(KeyBody):
    target_td: str = Field(..., min_length=10, max_length=600)
    from_td: str = Field(..., min_length=10, max_length=600)
    approved: bool = False


@router.get("/overview")
async def get_overview(key: str = "", light: bool = False) -> dict:
    """light=1 — 지표(GetMetricData) 없이: VS Code 를 열 때의 '밤사이 요약'처럼 자주 부르는 곳."""
    try:
        return await asyncio.to_thread(overview, key, light=light)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/plan")
async def post_plan(body: PlanBody) -> dict:
    m = _monitor(body.key)
    try:
        return await asyncio.to_thread(watcher.plan, session_for(m["region"]), region=m["region"], cluster=m["cluster"],
                                       service=m["service"], role=body.role)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/install")
async def post_install(body: InstallBody) -> dict:
    m = _monitor(body.key)
    if not body.approved:
        raise HTTPException(status_code=400, detail="승인 없이 설치하지 않습니다.")

    def work() -> dict:
        session = session_for(m["region"])
        current = ""
        try:
            current = str((session.client("ecs").describe_services(cluster=m["cluster"], services=[m["service"]])
                           .get("services") or [{}])[0].get("taskDefinition") or "")
        except Exception:  # noqa: BLE001 - install 이 다시 확인하고 알맞은 오류를 낸다
            pass
        stable = _stable_from_records(m, current) if current else ""
        result = watcher.install(session, region=m["region"], cluster=m["cluster"], service=m["service"], role=body.role,
                                 discord=body.discord, stable_td=stable, approved=True)
        if not stable:
            result["notes"].append("이전 배포 기록이 없어 밤 롤백 링크는 다음 배포부터 준비됩니다(감지 · 알림은 지금부터).")
        return result
    try:
        return await asyncio.to_thread(work)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/uninstall")
async def post_uninstall(body: ApproveBody) -> dict:
    m = _monitor(body.key)
    try:
        return await asyncio.to_thread(watcher.uninstall, session_for(m["region"]), region=m["region"], cluster=m["cluster"],
                                       service=m["service"], approved=body.approved)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/settings")
async def post_settings(body: SettingsBody) -> dict:
    m = _monitor(body.key)
    try:
        return await asyncio.to_thread(watcher.settings, session_for(m["region"]), region=m["region"], cluster=m["cluster"],
                                       service=m["service"], discord=body.discord, email=body.email)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/rollback")
async def post_rollback(body: RollbackBody) -> dict:
    m = _monitor(body.key)
    try:
        return await asyncio.to_thread(watcher.rollback, session_for(m["region"]), region=m["region"], cluster=m["cluster"],
                                       service=m["service"], target_td=body.target_td, from_td=body.from_td,
                                       approved=body.approved)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
