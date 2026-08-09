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
# 접두어가 "/ecs" 였다. 다른 라우터는 전부 "/api/..." 를 쓰는데 여기만
# 달라서, 디스코드 봇이 부르는 "/api/ecs/deploy" 가 404 였다.
# 리포 전체를 확인한 결과 "/ecs/..." 를 부르는 코드는 없었으므로
# (로그 그룹 이름 문자열만 걸린다) 깨질 호출자 없이 맞출 수 있다.
router = APIRouter(prefix="/api/ecs", tags=["ecs"])

# 인메모리 배포 레코드 저장소 (실제 환경에서는 SQLite/DB로 대체)
_deploy_records: Dict[str, ECSDeployRecord] = {}
_ecs_agent = ECSAgent()


#: 아직 끝나지 않은 배포로 볼 상태.
_ACTIVE_STATUSES = frozenset({ECSDeployStatus.PENDING, ECSDeployStatus.IN_PROGRESS})


def _active_deployment(cluster: str, service: str) -> Optional[ECSDeployRecord]:
    """같은 클러스터·서비스에서 아직 돌고 있는 배포. 없으면 None."""
    for record in _deploy_records.values():
        if record.status not in _ACTIVE_STATUSES:
            continue
        if record.cluster == cluster and record.service == service:
            return record
    return None


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
    # **같은 서비스에 배포가 이미 돌고 있으면 새로 시작하지 않는다.**
    #
    # 확장의 배포 버튼을 두 번 누르거나 재시도하면 예전에는 파이프라인이
    # 두 개 동시에 떴다. 둘 다 같은 태그로 빌드해 같은 ECR 리포에 올리고
    # 같은 ECS 서비스를 갱신하므로 docker/ECR 작업이 뒤엉킨다. 게다가
    # `/api/deploy/ecs/status` 는 **가장 최근에 시작된 것 하나만** 보여줘서,
    # 먼저 시작한 배포는 사용자 눈에서 사라진 채 계속 돌아간다.
    active = _active_deployment(request.cluster, request.service)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "deployment_in_progress",
                "message": (
                    f"{request.cluster}/{request.service} 에 이미 진행 중인 "
                    "배포가 있습니다. 끝난 뒤 다시 시도하세요."
                ),
                "deployment_id": active.deployment_id,
                "started_at": active.started_at.isoformat() if active.started_at else "",
            },
        )

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
    #
    # 이 블록은 예전에 잘못된 인자 이름(policy_path / input_data /
    # security_level)으로 evaluate() 를 불렀다. 실제 시그니처는
    # (action, level, context, ...) 라서 매번 TypeError 가 났고, 그걸 아래
    # `except Exception` 이 삼켜 "OPA에 연결할 수 없습니다"로 바꿔 내보냈다.
    # 결과: **모든 ECS 배포 요청이 항상 503.** 우리 코드의 타입 오류가
    # 외부 서비스 장애로 둔갑하는, 원인을 절대 못 찾는 형태였다.
    #
    # OPAClient.evaluate() 는 이미 내부에서 fail-closed 를 한다
    # (연결 실패 시 Level 3~4 는 deny, 1~2 는 allow). 그래서 여기서
    # 한 겹 더 감싸지 않는다 — 그 중복이 위 사고의 원인이었다.
    from core.opa_client import opa_client

    try:
        opa_result = await opa_client.evaluate(
            action="ecs_deploy",
            level=request.approval_level,
            context={
                "project_id": request.project_id,
                "image": request.image,
                "cluster": request.cluster,
                "region": request.region,
                "run_security_scan": request.run_security_scan,
                "generate_sbom": request.generate_sbom,
            },
            resource_type="ecs_service",
            resource_id=f"{request.cluster}/{request.service}",
        )
    except Exception as opa_exc:  # noqa: BLE001
        # 여기까지 왔다면 클라이언트 자체의 결함이다(시그니처·구현 오류).
        # 그걸 "OPA 장애"로 보고하면 사용자는 영원히 엉뚱한 곳을 본다.
        logger.error("OPA 평가 호출 자체가 실패했습니다", exc_info=True)
        record.status = ECSDeployStatus.FAILED
        record.error_message = f"정책 평가를 실행하지 못했습니다: {opa_exc}"
        raise HTTPException(
            status_code=500,
            detail={
                "error": "policy_evaluation_crashed",
                "message": f"정책 평가 중 내부 오류가 발생했습니다: {opa_exc}",
                "deployment_id": deployment_id,
            },
        ) from opa_exc

    # **"allow" 하나만 통과시킨다.**
    #
    # 예전에는 `decision.startswith("deny")` 로만 막았다. 그런데 결정값은
    # 다섯 가지다 — allow / allow_with_approval / deny /
    # deny_with_fix_suggestion / escalate_to_security. 앞의 조건은
    # `allow_with_approval` 과 `escalate_to_security` 를 통과시켜서,
    # **승인자가 필요하다는 결정과 보안 에스컬레이션 결정이 곧바로
    # 배포로 이어졌다.** 게이트를 통과시키는 조건은 화이트리스트여야 한다.
    decision = (opa_result.decision or "").strip()
    if decision != "allow":
        record.status = ECSDeployStatus.FAILED
        record.error_message = f"정책 통과 실패({decision}) — {opa_result.reason}"

        # OPA 에 못 닿아서 나온 거부와, 정책이 실제로 막은 거부는 다르다.
        # 같은 코드로 보고하면 사용자가 무엇을 해야 할지 알 수 없다.
        if not opa_result.opa_available:
            error_code, status_code = "opa_unavailable", 503
            message = "OPA에 연결할 수 없습니다. Level 3+ 배포는 차단됩니다."
        elif decision == "allow_with_approval":
            error_code, status_code = "approval_required", 403
            message = (
                f"이 배포는 승인이 필요합니다(필요 승인자 "
                f"{opa_result.required_approvers}명). 승인 절차를 거친 뒤 "
                "다시 시도하세요."
            )
        elif decision == "escalate_to_security":
            error_code, status_code = "security_escalation_required", 403
            message = "이 배포는 보안팀 검토가 필요합니다."
        else:
            error_code, status_code = "policy_denied", 403
            message = f"OPA 정책이 이 배포를 거부했습니다: {opa_result.reason}"

        raise HTTPException(
            status_code=status_code,
            detail={
                "error": error_code,
                "decision": decision,
                "message": message,
                "fix_suggestion": opa_result.fix_suggestion,
                "deployment_id": deployment_id,
            },
        )

    # 백그라운드에서 ECS 파이프라인 실행
    background_tasks.add_task(_run_deployment, deployment_id, request)

    logger.info(
        "ECS deployment started: id=%s project=%s cluster=%s service=%s image=%s",
        deployment_id, request.project_id, request.cluster, request.service, request.image,
    )
    return record


async def _run_deployment(deployment_id: str, request: ECSDeployRequest) -> None:
    """백그라운드 배포 태스크.

    저장소에 있는 **바로 그 기록 객체**를 에이전트에 넘긴다. 예전에는
    에이전트가 자기 기록을 따로 만들어 채우고 끝에 통째로 교체했는데,
    그동안 사이드바 폴링은 계속 초기 PENDING 만 봤다 — 진행 단계도
    로그도 배포가 끝날 때까지 하나도 안 보였다.
    """
    record = _deploy_records.get(deployment_id)
    try:
        result = await _ecs_agent.deploy(request, record=record)
        # deploy() 가 새 기록을 만들었을 경우(record=None) 에만 id 를 맞춘다.
        if result is not record:
            result.deployment_id = deployment_id
        _deploy_records[deployment_id] = result
        logger.info("배포 %s 종료: status=%s", deployment_id, result.status)
    except Exception as exc:
        logger.error("배포 %s 가 예외로 중단됨: %s", deployment_id, exc, exc_info=True)
        if record is not None:
            record.status = ECSDeployStatus.FAILED
            record.error_message = str(exc)
            record.completed_at = datetime.now(timezone.utc)


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

    project_id, cluster로 필터링 가능. 최신순(started_at 내림차순) 정렬.
    """
    records = list(_deploy_records.values())

    if project_id:
        records = [r for r in records if r.project_id == project_id]
    if cluster:
        records = [r for r in records if r.cluster == cluster]

    # 최신순 정렬
    # ECSDeployRecord 에는 created_at 이 없다 — 시작 시각이 started_at 이다.
    records.sort(key=lambda r: r.started_at, reverse=True)
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
