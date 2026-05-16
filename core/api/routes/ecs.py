"""
Local Core — Q3: ECS Deployment API Routes

설계서 §Q3-A (Must):
- POST /ecs/deploy          : ECS Rolling Update 파이프라인 시작
- GET  /ecs/deploy/{id}     : 배포 상태 조회
- GET  /ecs/deployments     : 최근 배포 목록
- POST /ecs/deploy/{id}/cancel : 진행 중인 배포 취소 요청

모든 요청은 Control Plane OPA 정책 통과 후 실행됨.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from core.agents.ecs_agent import ECSAgent
from core.schemas import (
    ECSDeployRecord,
    ECSDeployRequest,
    ECSDeployStatus,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ecs", tags=["ecs"])

# 인메모리 배포 레코드 저장소 (실제 환경에서는 SQLite/DB로 대체)
_deploy_records: Dict[str, ECSDeployRecord] = {}
_ecs_agent = ECSAgent()


# ---------------------------------------------------------------------------
# POST /ecs/deploy
# ---------------------------------------------------------------------------

@router.post("/deploy", response_model=ECSDeployRecord, status_code=202)
async def start_deployment(
    request: ECSDeployRequest,
    background_tasks: BackgroundTasks,
    req: Request,
) -> ECSDeployRecord:
    """
    ECS Rolling Update 파이프라인을 백그라운드로 시작합니다.

    파이프라인:
    1. Preflight 점검 (read-only IAM: ECS/ECR/IAM/CloudWatch)
    2. 보안 스캔 (Trivy critical=block, Hadolint error=block, gitleaks always-block)
    3. SBOM 생성 (Syft CycloneDX JSON)
    4. ECS Task Definition 등록
    5. update-service --force-new-deployment
    6. CloudWatch 폴링 + Circuit Breaker (5분/50%)
    7. 실패 시 rollback proposal (Level 3 승인 필요)

    OPA 정책 위반 시 400 반환 (fail-closed Level ≥ 3).
    """
    deployment_id = str(uuid4())

    # 초기 PENDING 레코드 등록
    record = ECSDeployRecord(
        deployment_id=deployment_id,
        project_id=request.project_id,
        cluster=request.cluster,
        service=request.service,
        region=request.region,
        image=request.image,
        status=ECSDeployStatus.PENDING,
    )
    _deploy_records[deployment_id] = record

    # OPA 정책 평가 (core/opa_client.py)
    try:
        from core.opa_client import opa_client

        policy_input = {
            "action": "ecs_deploy",
            "project_id": request.project_id,
            "image": request.image,
            "cluster": request.cluster,
            "region": request.region,
            "run_security_scan": request.run_security_scan,
            "generate_sbom": request.generate_sbom,
        }
        opa_result = await opa_client.evaluate(
            policy_path="recoder/deploy/allow",
            input_data=policy_input,
            security_level=request.security_level,
        )
        if not opa_result.get("result", {}).get("allow", True):
            record.status = ECSDeployStatus.FAILED
            record.error_message = "OPA 정책 거부 — 배포 권한 없음"
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "policy_denied",
                    "message": "OPA 정책이 이 배포를 거부했습니다",
                    "deployment_id": deployment_id,
                },
            )
    except HTTPException:
        raise
    except Exception as opa_exc:
        # OPA 연결 실패 → fail-closed (Level ≥ 3)
        if request.security_level >= 3:
            record.status = ECSDeployStatus.FAILED
            record.error_message = f"OPA 연결 실패 (fail-closed): {opa_exc}"
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "opa_unavailable",
                    "message": "OPA에 연결할 수 없습니다. Level 3+ 배포는 차단됩니다.",
                    "deployment_id": deployment_id,
                },
            )
        logger.warning("OPA 연결 실패 (Level %d, pass-through): %s", request.security_level, opa_exc)

    # 백그라운드에서 ECS 파이프라인 실행
    background_tasks.add_task(_run_deployment, deployment_id, request)

    logger.info(
        "ECS deployment started: id=%s project=%s cluster=%s service=%s image=%s",
        deployment_id, request.project_id, request.cluster, request.service, request.image,
    )
    return record


async def _run_deployment(deployment_id: str, request: ECSDeployRequest) -> None:
    """백그라운드 배포 태스크."""
    try:
        result = await _ecs_agent.deploy(request)
        result.deployment_id = deployment_id
        _deploy_records[deployment_id] = result
        logger.info(
            "Deployment %s finished: status=%s", deployment_id, result.status
        )
    except Exception as exc:
        logger.error("Deployment %s crashed: %s", deployment_id, exc, exc_info=True)
        if deployment_id in _deploy_records:
            _deploy_records[deployment_id].status = ECSDeployStatus.FAILED
            _deploy_records[deployment_id].error_message = str(exc)


# ---------------------------------------------------------------------------
# GET /ecs/deploy/{deployment_id}
# ---------------------------------------------------------------------------

@router.get("/deploy/{deployment_id}", response_model=ECSDeployRecord)
async def get_deployment_status(deployment_id: str) -> ECSDeployRecord:
    """
    특정 배포의 현재 상태를 조회합니다.

    VSCode Extension Sidebar에서 실시간 폴링에 사용합니다.
    """
    record = _deploy_records.get(deployment_id)
    if not record:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "deployment_id": deployment_id},
        )
    return record


# ---------------------------------------------------------------------------
# GET /ecs/deployments
# ---------------------------------------------------------------------------

@router.get("/deployments", response_model=List[ECSDeployRecord])
async def list_deployments(
    project_id: Optional[str] = Query(None, description="프로젝트 ID로 필터링"),
    cluster: Optional[str] = Query(None, description="ECS 클러스터 이름으로 필터링"),
    limit: int = Query(20, ge=1, le=100, description="최대 반환 개수"),
) -> List[ECSDeployRecord]:
    """
    최근 배포 목록을 반환합니다.

    project_id, cluster로 필터링 가능. 최신순(created_at 내림차순) 정렬.
    """
    records = list(_deploy_records.values())

    if project_id:
        records = [r for r in records if r.project_id == project_id]
    if cluster:
        records = [r for r in records if r.cluster == cluster]

    # 최신순 정렬
    records.sort(key=lambda r: r.created_at, reverse=True)
    return records[:limit]


# ---------------------------------------------------------------------------
# POST /ecs/deploy/{deployment_id}/cancel
# ---------------------------------------------------------------------------

@router.post("/deploy/{deployment_id}/cancel", response_model=ECSDeployRecord)
async def cancel_deployment(deployment_id: str) -> ECSDeployRecord:
    """
    진행 중인 배포를 취소 요청합니다.

    PENDING 또는 IN_PROGRESS 상태의 배포만 취소 가능합니다.
    실제 ECS 서비스를 즉시 중단하지는 않으며, rollback proposal이 생성됩니다.
    """
    record = _deploy_records.get(deployment_id)
    if not record:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "deployment_id": deployment_id},
        )

    if record.status not in (ECSDeployStatus.PENDING, ECSDeployStatus.IN_PROGRESS):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "cannot_cancel",
                "message": f"현재 상태({record.status})에서는 취소할 수 없습니다",
                "deployment_id": deployment_id,
            },
        )

    record.status = ECSDeployStatus.FAILED
    record.error_message = "사용자 요청으로 취소됨"
    record.completed_at = datetime.now(timezone.utc)

    logger.info("Deployment %s cancelled by user request", deployment_id)
    return record


# ---------------------------------------------------------------------------
# GET /ecs/preflight
# ---------------------------------------------------------------------------

@router.post("/preflight")
async def run_preflight_only(
    cluster: str,
    service: str,
    region: str,
    task_definition_family: str,
    ecr_repo: Optional[str] = None,
) -> dict:
    """
    배포 없이 Preflight 점검만 실행합니다.

    Extension에서 배포 전 사전 검증 용도로 사용합니다.
    """
    from core.agents.preflight_agent import PreflightAgent

    agent = PreflightAgent()
    try:
        report = await agent.run(
            cluster=cluster,
            service=service,
            region=region,
            task_definition_family=task_definition_family,
            ecr_repo=ecr_repo,
        )
        return {
            "passed": report.passed,
            "region": report.region,
            "cluster": report.cluster,
            "service": report.service,
            "checks": [c.model_dump() for c in report.checks],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"error": str(exc)})


# ---------------------------------------------------------------------------
# GET /ecs/scan
# ---------------------------------------------------------------------------

@router.post("/scan")
async def run_security_scan_only(image: str) -> dict:
    """
    배포 없이 보안 스캔만 실행합니다 (Trivy + Hadolint + gitleaks).

    Extension에서 PR 열기 전 사전 스캔 용도로 사용합니다.
    """
    from core.security_scan import security_scanner

    try:
        result = await security_scanner.scan_all(image=image)
        return {
            "scan_passed": result.scan_passed,
            "critical_count": result.critical_count,
            "hadolint_error_count": result.hadolint_error_count,
            "secret_count": result.secret_count,
            "findings": [f.model_dump() for f in result.findings],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"error": str(exc)})
