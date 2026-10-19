"""AWS 리소스 정리 API (2.0.26) — 목록(아무것도 안 바꿈) · 중지(되살릴 수 있음) · 완전 삭제(승인한 항목만)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
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
    return aws_cleanup.make_clients(session)


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
    #: 2.0.31 — 참이면 바로 작업 id 를 돌려주고 뒤에서 지운다(DB·CloudFront 정리는 15분을 넘길 수 있다).
    background: bool = False


# ---------------------------------------------------------------------------
# 2.0.31 뒤에서 도는 삭제 작업 — 요청 하나가 클라이언트 제한 시간(15분)을 넘기지 않게
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.RLock()
_KEEP_JOBS = 20
_LOADED = False
INTERRUPTED = ("코어가 정리 도중에 종료됐습니다(VS Code 를 닫았거나 코어를 다시 시작함). 이미 지운 것은 지워졌고, 남은 것은 "
               "목록을 다시 불러와 이어서 지우세요.")


def _jobs_file():
    from pathlib import Path
    import os
    return Path(os.environ.get("RECODER_HOME") or (Path.home() / ".recoder")) / "cleanup_jobs.json"


def _state_io():
    try:
        import state_io  # type: ignore
    except ImportError:  # pragma: no cover
        from core import state_io  # type: ignore
    return state_io


def _load_jobs() -> None:
    """2.0.32 — 작업 기록을 파일에 둔다. 코어가 정리 도중 꺼지면 다음 시작 때 그 작업을 '중단됨'으로 보여 준다
    (예전에는 메모리에만 있어 작업 번호가 404 가 되고, 화면은 무엇이 남았는지 알려 주지 못했다)."""
    global _LOADED
    with _JOBS_LOCK:
        if _LOADED:
            return
        _LOADED = True
        saved = _state_io().read_json(_jobs_file(), {})
        if not isinstance(saved, dict):
            return
        for jid, job in saved.items():
            if not isinstance(job, dict) or not re.fullmatch(r"[0-9a-f]{12}", str(jid)):
                continue
            if job.get("status") == "running":
                job.update(status="interrupted", error=INTERRUPTED, finished_at=job.get("finished_at") or time.time())
            _JOBS.setdefault(str(jid), job)


def _save_jobs() -> None:
    try:
        _state_io().write_json(_jobs_file(), _JOBS)
    except OSError as exc:
        logger.info("정리 작업 기록 저장 실패: %s", exc)


def start_job(region: str, ids: list[str], *, clients: Optional[dict] = None, records: Optional[list[dict]] = None) -> dict:
    """같은 리전에 도는 작업이 있으면 409. 작업 상태 dict 를 돌려준다(복사본)."""
    _load_jobs()
    with _JOBS_LOCK:
        if any(j["region"] == region and j["status"] == "running" for j in _JOBS.values()):
            raise HTTPException(status_code=409, detail="이 리전에서 정리가 이미 진행 중입니다 — 끝난 뒤 다시 시도하세요.")
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "region": region, "status": "running", "count": len(ids), "progress": {"done": 0, "total": len(ids)},
               "result": None, "error": "", "started_at": time.time(), "finished_at": None}
        _JOBS[job_id] = job
        for old in sorted(_JOBS, key=lambda k: _JOBS[k]["started_at"])[:-_KEEP_JOBS]:
            if _JOBS[old]["status"] != "running":
                _JOBS.pop(old, None)
        _save_jobs()

    def progress(p: dict) -> None:
        with _JOBS_LOCK:
            job["progress"] = dict(p)
            _save_jobs()

    def work() -> None:
        try:
            result = aws_cleanup.delete(clients if clients is not None else clients_for(region), ids, region=region,
                                        approved=True, records=records if records is not None else _records(region),
                                        on_progress=progress)
            with _JOBS_LOCK:
                job.update(status="done", result=result, progress={"done": job["progress"].get("total", len(ids)),
                                                                    "total": job["progress"].get("total", len(ids))})
        except Exception as exc:  # noqa: BLE001
            logger.warning("정리 작업 실패 %s: %s", job_id, exc)
            with _JOBS_LOCK:
                job.update(status="failed", error=_aws_error(exc).detail)
        finally:
            with _JOBS_LOCK:
                job["finished_at"] = time.time()
                _save_jobs()

    threading.Thread(target=work, name=f"recoder-cleanup-{job_id}", daemon=True).start()
    return job_view(job_id)


def job_view(job_id: str) -> dict:
    _load_jobs()
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="정리 작업을 찾지 못했습니다(코어를 다시 시작했다면 목록을 다시 불러오세요).")
        return json.loads(json.dumps(job))


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
    job = start_job(body.region, ids)   # 같은 작업 저장소에 올려 겹침(409)을 한곳에서 막는다
    if body.background:
        return job
    while True:  # 옛 확장(동기 응답을 기다림) 호환
        view = job_view(job["id"])
        if view["status"] == "done":
            return view["result"]
        if view["status"] == "failed":
            raise HTTPException(status_code=502, detail=view["error"] or "AWS 리소스를 지우지 못했습니다.")
        await asyncio.sleep(0.5)


@router.get("/regions")
async def regions() -> dict:
    """ReCoder 로 배포한 적 있는 리전(정리 화면의 리전 목록) — 정해 둔 몇 리전만 고를 수 있어 다른 리전의 리소스를 지울 수
    없던 것(2.0.32)."""
    found = sorted({str(r.get("region")) for r in deploy_records() if r.get("region") and re.fullmatch(_REGION, str(r.get("region")))})
    return {"regions": found}


@router.get("/jobs")
async def jobs(region: str = "") -> dict:
    """최근 정리 작업(새 것부터) — 화면을 다시 열었을 때 진행 중인 작업에 다시 붙고, 중단된 작업을 알린다(2.0.32)."""
    if region and not re.fullmatch(_REGION, region):
        raise HTTPException(status_code=400, detail="리전 형식이 올바르지 않습니다.")
    _load_jobs()
    with _JOBS_LOCK:
        items = [json.loads(json.dumps(j)) for j in _JOBS.values() if not region or j.get("region") == region]
    items.sort(key=lambda j: -float(j.get("started_at") or 0))
    return {"jobs": items[:10]}


@router.get("/jobs/{job_id}")
async def job(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(status_code=400, detail="작업 id 가 올바르지 않습니다.")
    return job_view(job_id)
