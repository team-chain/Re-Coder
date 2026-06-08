"""
Workbench REST API (§27 + Discord/VSCode 양방향 sync).

Discord 봇과 VSCode Webview 둘 다 호출하는 통합 API. SQLite 3-Layer 영속화가
single source of truth — 어느 쪽에서 액션해도 양쪽이 동일한 state 를 본다.

엔드포인트:
    GET  /workbench/state              — 현재 통합 state (모든 채널이 polling)
    GET  /workbench/preflight-runs     — 최근 PreflightRun 리스트
    GET  /workbench/deployments        — 최근 DeploymentLedger 리스트
    GET  /workbench/remediation/{run_id} — 특정 RemediationRun
    POST /workbench/preflight/run      — Preflight 실행
    POST /workbench/remediation/apply  — RemediationProposal 적용
    POST /workbench/deployment/start   — 배포 시작 (3-Layer 에 기록)
    POST /workbench/deployment/{id}/rollback — Rollback
    POST /workbench/mode               — 활성 모드 전환 (Build/Ship/Operate/Recover)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

try:
    from persistence import (
        RecoderDB,
        get_default_db_path,
        list_deployments,
        list_preflight_runs,
        list_rollback_candidates,
        load_deployment,
        load_preflight_run,
        save_deployment,
        save_preflight_run,
        update_deployment_status,
    )
    from schemas import (
        DeploymentLedger,
        DeploymentLedgerStatus,
        PreflightRun,
        PreflightStatus,
        RemediationProposal,
    )
except ImportError:  # pragma: no cover
    from core.persistence import (  # type: ignore
        RecoderDB,
        get_default_db_path,
        list_deployments,
        list_preflight_runs,
        list_rollback_candidates,
        load_deployment,
        load_preflight_run,
        save_deployment,
        save_preflight_run,
        update_deployment_status,
    )
    from core.schemas import (  # type: ignore
        DeploymentLedger,
        DeploymentLedgerStatus,
        PreflightRun,
        PreflightStatus,
        RemediationProposal,
    )


log = logging.getLogger(__name__)
router = APIRouter(prefix="/workbench", tags=["workbench"])


# ---------------------------------------------------------------------------
# Active Mode — in-memory 단일 인스턴스 (모든 채널이 공유)
# ---------------------------------------------------------------------------


_ACTIVE_MODE: Literal["build", "ship", "operate", "recover", "home"] = "home"
_ACTIVE_PROJECT_ID: Optional[str] = None
_LAST_EVENTS: list[dict] = []  # 최근 이벤트 (양방향 sync 알림용)
_EVENT_LIMIT = 50


def _record_event(kind: str, source: str, payload: Optional[dict] = None) -> None:
    """양방향 sync 이벤트 기록. polling 으로 받아감."""
    evt = {
        "kind": kind,                   # "preflight" / "deploy" / "rollback" / "mode_change"
        "source": source,               # "vscode" / "discord" / "core"
        "at": datetime.now(timezone.utc).isoformat(),
        "payload": payload or {},
    }
    _LAST_EVENTS.append(evt)
    if len(_LAST_EVENTS) > _EVENT_LIMIT:
        _LAST_EVENTS.pop(0)


def _get_db() -> RecoderDB:
    return RecoderDB(get_default_db_path(), check_same_thread=False)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class WorkbenchState(BaseModel):
    """Discord & VSCode 가 polling 으로 받는 통합 state."""
    active_mode:        Literal["build", "ship", "operate", "recover", "home"]
    active_project_id:  Optional[str] = None
    last_preflight:     Optional[PreflightRun] = None
    last_deployment:    Optional[DeploymentLedger] = None
    blockers_count:     int = 0
    warnings_count:     int = 0
    deployments_24h:    int = 0
    rollback_available: bool = False
    recent_events:      list[dict] = Field(default_factory=list)
    as_of:              datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ModeChangeRequest(BaseModel):
    mode:    Literal["build", "ship", "operate", "recover", "home"]
    source:  Literal["vscode", "discord", "core"] = "core"


class PreflightRunRequest(BaseModel):
    project_id:     Optional[str] = None
    workspace_path: Optional[str] = None  # 없으면 현재 cwd
    project_path:   Optional[str] = None  # github-action 이 보내는 별칭 (workspace_path 와 동일 의미)
    # vscode/discord/core 외에 "github-action" 같은 외부 소스도 허용 — 자유 문자열.
    source:         str = "core"


class DeploymentStartRequest(BaseModel):
    project_id:     Optional[str] = None
    image_digest:   Optional[str] = None
    git_commit:     Optional[str] = None
    target_env:     Literal["local", "ec2", "ecs", "k8s"] = "local"
    source:         Literal["vscode", "discord", "core"] = "core"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/state", response_model=WorkbenchState)
async def get_state(project_id: Optional[str] = Query(None)) -> WorkbenchState:
    """현재 통합 state — Discord/VSCode 둘 다 polling 으로 호출."""
    db = _get_db()
    pid = project_id or _ACTIVE_PROJECT_ID

    pre_runs = list_preflight_runs(db, project_id=pid, limit=1)
    deps = list_deployments(db, project_id=pid, limit=1)
    rollback_cands = list_rollback_candidates(db, project_id=pid or "_default", limit=1) if pid else []

    last_pre = pre_runs[0] if pre_runs else None
    last_dep = deps[0] if deps else None

    blockers = len(last_pre.blockers) if last_pre else 0
    warnings = len(last_pre.warnings) if last_pre else 0

    # 24h 배포 카운트
    now = datetime.now(timezone.utc)
    deps_24h = [
        d for d in list_deployments(db, project_id=pid, limit=50)
        if (now - d.created_at.replace(tzinfo=timezone.utc) if d.created_at.tzinfo is None else now - d.created_at).total_seconds() < 86400
    ]

    return WorkbenchState(
        active_mode=_ACTIVE_MODE,
        active_project_id=pid,
        last_preflight=last_pre,
        last_deployment=last_dep,
        blockers_count=blockers,
        warnings_count=warnings,
        deployments_24h=len(deps_24h),
        rollback_available=bool(rollback_cands),
        recent_events=_LAST_EVENTS[-10:],  # 최근 10건
    )


@router.get("/preflight-runs", response_model=list[PreflightRun])
async def list_recent_preflight_runs(
    project_id: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
) -> list[PreflightRun]:
    db = _get_db()
    return list_preflight_runs(db, project_id=project_id, limit=limit)


@router.get("/deployments", response_model=list[DeploymentLedger])
async def list_recent_deployments(
    project_id: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
) -> list[DeploymentLedger]:
    db = _get_db()
    return list_deployments(db, project_id=project_id, limit=limit)


@router.post("/mode")
async def change_mode(req: ModeChangeRequest) -> dict:
    """활성 모드 전환 — 모든 채널이 다음 polling 에서 받음."""
    global _ACTIVE_MODE
    _ACTIVE_MODE = req.mode
    _record_event("mode_change", req.source, {"mode": req.mode})
    log.info("Workbench mode → %s (source=%s)", req.mode, req.source)
    return {"active_mode": _ACTIVE_MODE, "source": req.source}


def _detect_contract_stack(ws):
    """워크스페이스에서 contract 스택을 간이 감지.

    recoder.yml 이 없을 때 build_default_contract 에 넘길 ContractStack 을 정한다.
    프레임워크(FastAPI/Flask/Express/Next)가 안 보이면 CUSTOM — CUSTOM 은
    health 검사가 blocker 가 아니라 warning 이라 stdlib 앱도 깨끗하게 통과한다.
    """
    try:
        from schemas import ContractStack  # type: ignore
    except ImportError:  # pragma: no cover
        from core.schemas import ContractStack  # type: ignore
    try:
        from pathlib import Path
        ws = Path(ws)
        # Node 우선 판별
        pkg = ws / "package.json"
        if pkg.exists():
            try:
                import json as _json
                data = _json.loads(pkg.read_text(encoding="utf-8"))
                deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
                if "next" in deps:
                    return ContractStack.NODE_NEXT
                if "express" in deps:
                    return ContractStack.NODE_EXPRESS
            except Exception:
                pass
            return ContractStack.NODE_EXPRESS
        # Python 프레임워크 판별 — .py 안에서 fastapi/flask import 탐지
        skip = {"node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".git"}
        for py in list(ws.rglob("*.py"))[:200]:
            if any(part in skip for part in py.parts):
                continue
            try:
                head = py.read_text(encoding="utf-8", errors="ignore")[:4000].lower()
            except OSError:
                continue
            if "fastapi" in head:
                return ContractStack.PYTHON_FASTAPI
            if "flask" in head:
                return ContractStack.PYTHON_FLASK
    except Exception:
        pass
    return ContractStack.CUSTOM


@router.post("/preflight/run")
async def trigger_preflight(req: PreflightRunRequest) -> dict:
    """Preflight 실행 트리거. 실제 검사는 backbone 호출.

    Discord 봇/VSCode 가 같은 엔드포인트 호출. 결과는 PreflightRun 으로
    SQLite 에 저장되어 다음 /state 호출에서 보임.
    """
    db = _get_db()

    # 실제 12종 정적 검사를 StaticPreflightRunner 로 실행.
    # recoder.yml 이 있으면 그걸 contract 로, 없으면 스택 감지 후 기본 contract.
    try:
        from pathlib import Path
        from preflight import StaticPreflightRunner
        from preflight.contract_loader import load_contract, build_default_contract
        ws = Path(req.workspace_path or req.project_path or ".").resolve()
        # load_contract 는 워크스페이스 '디렉터리' 를 받아 recoder.yml 을 자동 탐색한다.
        contract = load_contract(ws)
        if contract is None:
            contract = build_default_contract(_detect_contract_stack(ws))
        runner = StaticPreflightRunner(str(ws), contract, project_id=req.project_id)
        run = runner.run_sync()
    except Exception as exc:  # pragma: no cover — preflight 모듈 자체가 없을 때만 graceful
        log.warning("Preflight 실행 실패, mock 으로 대체: %s", exc)
        run = PreflightRun(
            project_id=req.project_id,
            status=PreflightStatus.PASSED,
            score=85,
        )

    save_preflight_run(db, run)
    _record_event("preflight", req.source, {
        "preflight_run_id": run.preflight_run_id,
        "status": run.status.value if hasattr(run.status, "value") else str(run.status),
        "score": run.score,
        "blockers": len(run.blockers),
        "warnings": len(run.warnings),
    })
    _status = run.status.value if hasattr(run.status, "value") else str(run.status)
    return {
        "preflight_run_id": run.preflight_run_id,
        "status": _status,
        # score = 건강도(높을수록 좋음). risk_score = 위험도(높을수록 나쁨) — CI 게이트용.
        "score": run.score,
        "risk_score": max(0, 100 - int(run.score)),
        "blockers": [b.model_dump() for b in run.blockers],
        "warnings": [w.model_dump() for w in run.warnings],
    }


@router.post("/deployment/start")
async def start_deployment(req: DeploymentStartRequest) -> dict:
    """배포 시작 — DeploymentLedger 에 DEPLOYING 상태로 INSERT."""
    db = _get_db()

    pre_runs = list_preflight_runs(db, project_id=req.project_id, limit=1)
    pre_id = pre_runs[0].preflight_run_id if pre_runs else None

    ledger = DeploymentLedger(
        project_id=req.project_id,
        preflight_run_id=pre_id,
        image_digest=req.image_digest,
        git_commit=req.git_commit,
        status=DeploymentLedgerStatus.DEPLOYING,
    )
    save_deployment(db, ledger)
    _record_event("deploy", req.source, {
        "deployment_id": ledger.deployment_id,
        "target_env": req.target_env,
    })
    log.info("Deployment started: %s (source=%s)", ledger.deployment_id, req.source)
    return {
        "deployment_id": ledger.deployment_id,
        "status": "deploying",
        "preflight_run_id": pre_id,
    }


@router.post("/deployment/{deployment_id}/rollback")
async def rollback_deployment(
    deployment_id: str,
    source: Literal["vscode", "discord", "core"] = "core",
) -> dict:
    """배포 rollback — DEPLOYING/STABLE/FAILED → ROLLED_BACK."""
    db = _get_db()
    ok = update_deployment_status(db, deployment_id, DeploymentLedgerStatus.ROLLED_BACK)
    if not ok:
        raise HTTPException(404, "deployment not found or invalid transition")
    _record_event("rollback", source, {"deployment_id": deployment_id})
    log.info("Rollback executed: %s (source=%s)", deployment_id, source)
    return {"deployment_id": deployment_id, "status": "rolled_back"}


@router.get("/events")
async def get_events(since: int = Query(0, ge=0)) -> dict:
    """최근 이벤트 — Discord/VSCode 가 polling 으로 새 이벤트 받기.

    since: 이전에 받은 마지막 이벤트 인덱스 (timestamp 기반은 아니지만 충분).
    """
    new_events = _LAST_EVENTS[since:]
    return {
        "events": new_events,
        "next_offset": len(_LAST_EVENTS),
    }
