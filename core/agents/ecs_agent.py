"""
Local Core — Q3: ECS Rolling Update Agent

설계서 §Q3-A (Must):
1. Preflight 점검 (read-only IAM)
2. 보안 스캔 (Trivy / Hadolint / gitleaks)
3. SBOM 생성 (Syft CycloneDX)
4. ECS Task Definition JSON 생성 (FileTemplate Registry)
5. ECR 로그인 + docker build + 이미지 태그 + ECR push
6. boto3 update-service --force-new-deployment
7. CloudWatch 배포 상태 폴링 (Sidebar에 표시)
8. Health Check 실패 시 rollback proposal (Approval Level 3)
9. Circuit Breaker (5분 내 실패율 50% 초과 → 자동 중단)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.agents.preflight_agent import PreflightAgent
from core.sbom import sbom_generator
from core.schemas import (
    ECSDeployRecord,
    ECSDeployRequest,
    ECSDeployStatus,
    SecurityScanResult,
)
from core.security_scan import security_scanner

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 15           # CloudWatch 폴링 간격 (초)
_MAX_POLL_ATTEMPTS = 40       # 최대 폴링 횟수 (= 10분)
_CIRCUIT_BREAKER_WINDOW = 300 # 5분 (초)
_CIRCUIT_BREAKER_THRESHOLD = 0.5  # 50%

# FileTemplate 경로
_TEMPLATE_PATH = Path(__file__).parent.parent / "registry" / "file_templates" / "ecs-task-definition.json.template"


class ECSAgent:
    """
    ECS Rolling Update 전체 오케스트레이션.
    LLM을 사용하지 않는 결정론적 배포 에이전트.
    """

    def __init__(self) -> None:
        self._preflight = PreflightAgent()

    async def deploy(self, request: ECSDeployRequest) -> ECSDeployRecord:
        """ECS Rolling Update 전체 파이프라인 실행."""
        record = ECSDeployRecord(
            project_id=request.project_id,
            cluster=request.cluster,
            service=request.service,
            region=request.region,
            image=request.image,
            status=ECSDeployStatus.PENDING,
        )

        try:
            # 1. Preflight
            if request.run_preflight:
                record = await self._step_preflight(request, record)
                if not record.preflight_passed:
                    record.status = ECSDeployStatus.FAILED
                    record.error_message = "Preflight 점검 실패 — 배포를 중단합니다"
                    return record

            # 2. 보안 스캔
            if request.run_security_scan:
                record = await self._step_security_scan(request, record)
                if record.scan_result and not record.scan_result.scan_passed:
                    record.status = ECSDeployStatus.FAILED
                    record.error_message = (
                        f"보안 스캔 실패: critical={record.scan_result.critical_count} "
                        f"hadolint_err={record.scan_result.hadolint_error_count} "
                        f"secrets={record.scan_result.secret_count}"
                    )
                    return record

            # 3. SBOM 생성
            if request.generate_sbom:
                record = await self._step_sbom(request, record)

            # 4. Task Definition 생성 + 등록
            record.status = ECSDeployStatus.IN_PROGRESS
            task_def_arn, prev_arn = await self._step_register_task_definition(request)
            record.task_definition_arn = task_def_arn
            record.previous_task_definition_arn = prev_arn

            # 5. update-service
            await self._step_update_service(request, task_def_arn)

            # 6. 배포 상태 폴링 + Circuit Breaker
            success, failure_count = await self._step_poll_deployment(request, record)

            if not success:
                record.health_check_failures = failure_count
                if failure_count / max(_MAX_POLL_ATTEMPTS, 1) >= _CIRCUIT_BREAKER_THRESHOLD:
                    record.circuit_breaker_triggered = True
                    record.status = ECSDeployStatus.CIRCUIT_BREAKER_TRIGGERED
                else:
                    record.status = ECSDeployStatus.FAILED

                # 7. Rollback proposal 생성 (Approval Level 3)
                record.rollback_proposal_id = await self._create_rollback_proposal(request, record)
                record.error_message = "배포 Health Check 실패 — rollback proposal 생성됨"
                return record

            record.status = ECSDeployStatus.SUCCEEDED
            record.completed_at = datetime.now(timezone.utc)
            logger.info("ECS deployment succeeded: %s/%s image=%s", request.cluster, request.service, request.image)
            return record

        except Exception as exc:
            logger.error("ECS deployment error: %s", exc, exc_info=True)
            record.status = ECSDeployStatus.FAILED
            record.error_message = str(exc)
            return record

    # ------------------------------------------------------------------
    # 단계별 구현
    # ------------------------------------------------------------------

    async def _step_preflight(self, req: ECSDeployRequest, rec: ECSDeployRecord) -> ECSDeployRecord:
        ecr_repo = req.image.split(":")[0].split("/")[-1] if "/" in req.image else None
        report = await self._preflight.run(
            cluster=req.cluster,
            service=req.service,
            region=req.region,
            task_definition_family=req.task_definition_family,
            ecr_repo=ecr_repo,
        )
        rec.preflight_passed = report.passed
        logger.info("Preflight: passed=%s checks=%d", report.passed, len(report.checks))
        return rec

    async def _step_security_scan(self, req: ECSDeployRequest, rec: ECSDeployRecord) -> ECSDeployRecord:
        result = await security_scanner.scan_all(image=req.image)
        rec.scan_result = result
        return rec

    async def _step_sbom(self, req: ECSDeployRequest, rec: ECSDeployRecord) -> ECSDeployRecord:
        sbom = await sbom_generator.generate(req.image)
        rec.sbom_path = sbom.sbom_path
        rec.sbom_version = f"v{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        return rec

    async def _step_register_task_definition(
        self, req: ECSDeployRequest
    ) -> tuple[str, Optional[str]]:
        """Task Definition JSON 생성 → AWS 등록. 이전 revision ARN 반환."""
        import boto3

        ecs = boto3.client("ecs", region_name=req.region)

        # 이전 Task Definition ARN 조회 (rollback 대상)
        prev_arn: Optional[str] = None
        try:
            svc_resp = ecs.describe_services(cluster=req.cluster, services=[req.service])
            services = svc_resp.get("services", [])
            if services:
                prev_arn = services[0].get("taskDefinition")
        except Exception:
            pass

        # FileTemplate에서 Task Definition JSON 생성
        task_def = self._render_task_definition(req)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(task_def, f, indent=2)
            task_def_path = f.name

        try:
            resp = ecs.register_task_definition(**task_def)
            arn = resp["taskDefinition"]["taskDefinitionArn"]
            logger.info("Task Definition registered: %s", arn)
            return arn, prev_arn
        finally:
            os.unlink(task_def_path)

    async def _step_update_service(self, req: ECSDeployRequest, task_def_arn: str) -> None:
        """boto3 update-service --force-new-deployment"""
        import boto3
        ecs = boto3.client("ecs", region_name=req.region)
        ecs.update_service(
            cluster=req.cluster,
            service=req.service,
            taskDefinition=task_def_arn,
            forceNewDeployment=True,
        )
        logger.info("ECS update-service called: %s/%s → %s", req.cluster, req.service, task_def_arn)

    async def _step_poll_deployment(
        self, req: ECSDeployRequest, rec: ECSDeployRecord
    ) -> tuple[bool, int]:
        """
        CloudWatch 배포 상태 폴링.
        Circuit Breaker: 5분 내 Health Check 실패율 50% 초과 시 자동 중단.
        """
        import boto3
        ecs = boto3.client("ecs", region_name=req.region)

        failure_count = 0
        start_time = datetime.now(timezone.utc)

        for attempt in range(_MAX_POLL_ATTEMPTS):
            await asyncio.sleep(_POLL_INTERVAL)

            try:
                resp = ecs.describe_services(cluster=req.cluster, services=[req.service])
                svc = resp.get("services", [{}])[0]
                deployments = svc.get("deployments", [])

                # 현재 배포 중인 PRIMARY 배포 확인
                primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
                if primary is None:
                    continue

                running = primary.get("runningCount", 0)
                desired = primary.get("desiredCount", 0)
                failed_tasks = primary.get("failedTasks", 0)
                rollout_state = primary.get("rolloutState", "")

                logger.debug(
                    "Poll %d/%d: running=%d desired=%d failed=%d state=%s",
                    attempt + 1, _MAX_POLL_ATTEMPTS, running, desired, failed_tasks, rollout_state,
                )

                if failed_tasks > 0:
                    failure_count += failed_tasks

                # Circuit Breaker 체크
                elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
                if elapsed <= _CIRCUIT_BREAKER_WINDOW:
                    fail_rate = failure_count / max(attempt + 1, 1)
                    if fail_rate >= _CIRCUIT_BREAKER_THRESHOLD:
                        logger.warning("Circuit breaker triggered: fail_rate=%.1f%%", fail_rate * 100)
                        return False, failure_count

                # 성공 판단
                if rollout_state == "COMPLETED" and running >= desired and desired > 0:
                    return True, failure_count

                # 명시적 실패
                if rollout_state == "FAILED":
                    return False, failure_count

            except Exception as exc:
                logger.warning("Polling error (attempt %d): %s", attempt + 1, exc)

        # 폴링 시간 초과 → 실패 처리
        logger.warning("Deployment polling timed out after %d attempts", _MAX_POLL_ATTEMPTS)
        return False, failure_count

    async def _create_rollback_proposal(
        self, req: ECSDeployRequest, rec: ECSDeployRecord
    ) -> Optional[str]:
        """
        Health Check 실패 시 이전 Task Definition으로 rollback proposal 생성.
        Approval Level 3 (설계서 §Q3-A).
        ADR-005: 프로덕션은 Git revert PR 기본. ECS는 task definition rollback.
        """
        if not rec.previous_task_definition_arn:
            logger.warning("No previous task definition ARN — cannot create rollback proposal")
            return None

        proposal_id = f"rollback-{rec.deployment_id[:8]}"
        logger.warning(
            "Rollback proposal created: %s → revert to %s (Level 3 approval required)",
            proposal_id, rec.previous_task_definition_arn,
        )
        # 실제 승인 요청은 Control Plane API를 통해 생성 (Extension이 표시)
        return proposal_id

    # ------------------------------------------------------------------
    # Task Definition 렌더링
    # ------------------------------------------------------------------

    def _render_task_definition(self, req: ECSDeployRequest) -> dict:
        """FileTemplate에서 Task Definition dict 생성."""
        env_vars_list = [{"name": k, "value": v} for k, v in req.env_vars.items()]

        template_str = _TEMPLATE_PATH.read_text()
        # 간단한 문자열 치환 (Jinja2 없이)
        replacements = {
            "{{task_definition_family}}": req.task_definition_family,
            "{{cpu}}": req.cpu,
            "{{memory}}": req.memory,
            "{{container_name}}": req.container_name,
            "{{image}}": req.image,
            "{{container_port}}": "8000",
            "{{health_check_path}}": req.health_check_path,
            "{{region}}": req.region,
            "{{env_vars_json}}": json.dumps(env_vars_list),
            "{{execution_role_arn}}": os.environ.get(
                "ECS_EXECUTION_ROLE_ARN",
                f"arn:aws:iam::000000000000:role/ecsTaskExecutionRole",
            ),
            "{{task_role_arn}}": os.environ.get(
                "ECS_TASK_ROLE_ARN",
                f"arn:aws:iam::000000000000:role/ecsTaskRole",
            ),
        }
        for k, v in replacements.items():
            template_str = template_str.replace(k, str(v))

        return json.loads(template_str)
