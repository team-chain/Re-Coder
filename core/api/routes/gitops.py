"""
Local Core — Q4: GitOps API Routes

엔드포인트:
- POST /gitops/sync               : ArgoCD Application 동기화
- GET  /gitops/sync/{id}          : 동기화 상태 조회
- GET  /gitops/apps/{app}/status  : ArgoCD Application 현재 상태
- POST /gitops/rollback-pr        : Git revert PR 자동 생성 (ADR-005)
- GET  /gitops/rollback-pr/{id}   : rollback PR 상태 조회
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import Header, APIRouter, BackgroundTasks, HTTPException, Query

from core.agents.argocd_agent import argocd_agent
from core.agents.rollback_pr_agent import rollback_pr_agent
from core.schemas import (
    ArgoSyncRecord,
    ArgoSyncRequest,
    RollbackPRRecord,
    RollbackPRRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/gitops", tags=["gitops"])

# 인메모리 레코드 저장소
_sync_records: Dict[str, ArgoSyncRecord] = {}
_pr_records: Dict[str, RollbackPRRecord] = {}


# ---------------------------------------------------------------------------
# ArgoCD 동기화
# ---------------------------------------------------------------------------

@router.post("/sync", response_model=ArgoSyncRecord, status_code=202)
async def sync_argocd_app(
    request: ArgoSyncRequest,
    background_tasks: BackgroundTasks,
) -> ArgoSyncRecord:
    """
    ArgoCD Application 동기화를 백그라운드로 실행합니다.

    ADR-009: Q4 배포 표준 경로.
    동기화 실패 시 rollback_triggered=True → rollback PR 생성 권장.
    """
    from core.schemas import ArgoSyncPhase
    record = ArgoSyncRecord(
        project_id=request.project_id,
        app_name=request.app_name,
        argocd_server=request.argocd_server,
        target_revision=request.target_revision,
        sync_phase=ArgoSyncPhase.UNKNOWN,
    )
    _sync_records[record.sync_id] = record

    background_tasks.add_task(_run_argocd_sync, record.sync_id, request)

    logger.info(
        "ArgoCD sync started: id=%s app=%s server=%s",
        record.sync_id, request.app_name, request.argocd_server,
    )
    return record


async def _run_argocd_sync(sync_id: str, request: ArgoSyncRequest) -> None:
    try:
        result = await argocd_agent.sync(request)
        result.sync_id = sync_id
        _sync_records[sync_id] = result
    except Exception as exc:
        logger.error("ArgoCD sync %s crashed: %s", sync_id, exc, exc_info=True)
        from core.schemas import ArgoSyncPhase
        if sync_id in _sync_records:
            _sync_records[sync_id].sync_phase = ArgoSyncPhase.SYNC_FAILED
            _sync_records[sync_id].error_message = str(exc)


@router.get("/sync/{sync_id}", response_model=ArgoSyncRecord)
async def get_sync_status(sync_id: str) -> ArgoSyncRecord:
    """ArgoCD 동기화 상태를 조회합니다."""
    record = _sync_records.get(sync_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": "not_found", "sync_id": sync_id})
    return record


@router.get("/syncs", response_model=List[ArgoSyncRecord])
async def list_syncs(
    project_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> List[ArgoSyncRecord]:
    """최근 ArgoCD 동기화 목록을 반환합니다."""
    records = list(_sync_records.values())
    if project_id:
        records = [r for r in records if r.project_id == project_id]
    records.sort(key=lambda r: r.started_at, reverse=True)
    return records[:limit]


@router.get("/apps/{app_name}/status")
async def get_app_status(
    app_name: str,
    argocd_server: str = Query(..., description="ArgoCD 서버 주소"),
    # 토큰은 **헤더로만** 받는다. 쿼리스트링에 실으면 uvicorn 액세스 로그·
    # 프록시·브라우저 히스토리에 클러스터 배포 권한 토큰이 평문으로 남는다.
    argocd_token: str = Header(..., alias="X-ArgoCD-Token",
                               description="ArgoCD API 토큰 (헤더)"),
) -> dict:
    """ArgoCD Application의 현재 상태를 즉시 조회합니다."""
    try:
        state = await argocd_agent.get_app_status(app_name, argocd_server, argocd_token)
        sync = state.get("status", {}).get("sync", {})
        health = state.get("status", {}).get("health", {})
        return {
            "app_name": app_name,
            "sync_status": sync.get("status", "Unknown"),
            "health_status": health.get("status", "Unknown"),
            "revision": sync.get("revision"),
            "message": health.get("message", ""),
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": str(exc)})


# ---------------------------------------------------------------------------
# Rollback PR (ADR-005)
# ---------------------------------------------------------------------------

@router.post("/rollback-pr", response_model=RollbackPRRecord, status_code=202)
async def create_rollback_pr(
    request: RollbackPRRequest,
    background_tasks: BackgroundTasks,
) -> RollbackPRRecord:
    """
    Git revert PR을 자동 생성합니다.

    ADR-005: 프로덕션 rollback = Git revert PR 기본 경로.
    ADR-006: ArgoCD 직접 rollback은 Severity 1만 허용.
    PR 생성 후 Level 3 승인 (2인) 필요.
    """
    record = RollbackPRRecord(
        project_id=request.project_id,
        repo_full_name=f"{request.repo_owner}/{request.repo_name}",
        target_commit_sha=request.target_commit_sha,
        revert_branch=f"revert/{request.target_commit_sha[:8]}-auto",
    )
    _pr_records[record.pr_id] = record

    background_tasks.add_task(_run_create_rollback_pr, record.pr_id, request)

    logger.info(
        "Rollback PR started: id=%s repo=%s/%s sha=%s",
        record.pr_id, request.repo_owner, request.repo_name, request.target_commit_sha[:8],
    )
    return record


async def _run_create_rollback_pr(pr_id: str, request: RollbackPRRequest) -> None:
    try:
        result = await rollback_pr_agent.create_rollback_pr(request)
        result.pr_id = pr_id
        _pr_records[pr_id] = result
    except Exception as exc:
        logger.error("Rollback PR %s crashed: %s", pr_id, exc, exc_info=True)
        if pr_id in _pr_records:
            _pr_records[pr_id].status = "failed"
            _pr_records[pr_id].error_message = str(exc)


@router.get("/rollback-pr/{pr_id}", response_model=RollbackPRRecord)
async def get_rollback_pr_status(pr_id: str) -> RollbackPRRecord:
    """Rollback PR 상태를 조회합니다."""
    record = _pr_records.get(pr_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": "not_found", "pr_id": pr_id})
    return record
