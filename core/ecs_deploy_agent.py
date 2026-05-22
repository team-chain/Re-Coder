"""
ecs_deploy_agent.py — ECS Fargate Rolling Update 배포 에이전트 (설계서 §Q3-A)

배포 흐름:
  1. Docker 이미지 빌드 (로컬)
  2. ECR 로그인 → 이미지 push
  3. ECS Task Definition 새 revision 등록
  4. ECS Service update-service --force-new-deployment (boto3)
  5. CloudWatch 기반 배포 상태 폴링
  6. Circuit Breaker: 5분 내 Health Check 실패율 50% 초과 시 자동 중단
  7. 실패 시 이전 Task Definition으로 rollback proposal 생성 (Approval Level 3)

필요 환경변수:
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
  ECR_REGISTRY   — ECR 레지스트리 URL
  ECS_CLUSTER    — ECS 클러스터 이름
  ECS_SERVICE    — ECS 서비스 이름
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── 타임아웃 / 임계값 상수 ────────────────────────────────────────────
_DOCKER_BUILD_TIMEOUT  = 900   # 15분
_ECR_PUSH_TIMEOUT      = 600   # 10분
_DEPLOY_POLL_INTERVAL  = 15    # 초
_DEPLOY_POLL_TIMEOUT   = 600   # 10분 (최대 대기)
_CB_WINDOW_SECONDS     = 300   # Circuit Breaker 관찰 창: 5분
_CB_FAILURE_THRESHOLD  = 0.5   # 실패율 50% 초과 시 중단


@dataclass
class ECSDeployConfig:
    """ECS Fargate 배포에 필요한 설정값 묶음."""
    ecr_registry:   str          # 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com
    ecs_cluster:    str          # ECS 클러스터 이름
    ecs_service:    str          # ECS 서비스 이름
    aws_region:     str = "ap-northeast-2"
    container_name: str = "app"  # Task Definition 내 컨테이너 이름
    container_port: int = 8000
    cpu:            str = "256"  # Fargate CPU units
    memory:         str = "512"  # Fargate memory MiB
    env_vars:       list[dict] = field(default_factory=list)  # [{"name": k, "value": v}]

    @classmethod
    def from_env(cls) -> "ECSDeployConfig":
        """환경변수에서 설정 로드. 필수값 없으면 ValueError."""
        registry = os.getenv("ECR_REGISTRY", "").strip()
        cluster  = os.getenv("ECS_CLUSTER", "").strip()
        service  = os.getenv("ECS_SERVICE", "").strip()

        if not registry:
            raise ValueError("ECR_REGISTRY 환경변수가 설정되지 않았습니다.")
        if not cluster:
            raise ValueError("ECS_CLUSTER 환경변수가 설정되지 않았습니다.")
        if not service:
            raise ValueError("ECS_SERVICE 환경변수가 설정되지 않았습니다.")

        region = (
            os.getenv("AWS_DEFAULT_REGION")
            or os.getenv("AWS_REGION")
            or os.getenv("BEDROCK_REGION")
            or "ap-northeast-2"
        )

        return cls(
            ecr_registry=registry,
            ecs_cluster=cluster,
            ecs_service=service,
            aws_region=region,
            container_name=os.getenv("ECS_CONTAINER_NAME", "app"),
            container_port=int(os.getenv("ECS_CONTAINER_PORT", "8000")),
            cpu=os.getenv("ECS_CPU", "256"),
            memory=os.getenv("ECS_MEMORY", "512"),
        )


@dataclass
class ECSDeployResult:
    """ECS 배포 결과."""
    success:             bool
    image_uri:           str = ""
    task_definition_arn: str = ""   # 배포된 Task Definition ARN
    prev_task_def_arn:   str = ""   # 롤백 후보 (이전 revision)
    rollback_required:   bool = False
    error:               str = ""
    logs:                list[str] = field(default_factory=list)
    deployed_at:         str = ""


class ECSDeployAgent:
    """
    ECS Fargate Rolling Update 배포 에이전트.

    모든 public 메서드는 동기. server.py 에서 asyncio.to_thread 로 호출.
    """

    # ── 내부 헬퍼 ────────────────────────────────────────────────────

    def _run(
        self,
        args: list[str],
        cwd: Optional[str] = None,
        timeout: int = 60,
    ) -> tuple[int, str, str]:
        """subprocess.run 래퍼. (returncode, stdout, stderr)"""
        try:
            proc = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=os.environ.copy(),
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"타임아웃 ({timeout}s)"
        except FileNotFoundError as e:
            return -1, "", f"명령을 찾을 수 없음: {e}"
        except Exception as e:
            return -1, "", str(e)

    def _boto3_ecs(self, config: ECSDeployConfig):
        """boto3 ECS 클라이언트 반환."""
        import boto3
        return boto3.client("ecs", region_name=config.aws_region)

    def _boto3_cw(self, config: ECSDeployConfig):
        """boto3 CloudWatch 클라이언트 반환."""
        import boto3
        return boto3.client("cloudwatch", region_name=config.aws_region)

    # ── Step 1: Docker 이미지 빌드 ───────────────────────────────────

    def build_image(self, workspace_path: str, image_tag: str) -> tuple[bool, str]:
        """docker build -t {image_tag} ."""
        from pathlib import Path
        if not (Path(workspace_path) / "Dockerfile").exists():
            return False, f"Dockerfile 없음: {workspace_path}/Dockerfile"

        logger.info(f"[ecs_deploy] docker build: {image_tag}")
        rc, out, err = self._run(
            ["docker", "build", "-t", image_tag, "."],
            cwd=workspace_path,
            timeout=_DOCKER_BUILD_TIMEOUT,
        )
        if rc != 0:
            return False, f"docker build 실패:\n{err or out}"
        return True, image_tag

    # ── Step 2: ECR 로그인 + push ────────────────────────────────────

    def ecr_push(
        self,
        local_image: str,
        registry: str,
        repo_name: str,
        tag: str,
        region: str,
    ) -> tuple[bool, str]:
        """ECR 로그인 → 레포지토리 생성(없으면) → docker push. ECR URI 반환."""
        # ECR 토큰 발급
        rc, token, err = self._run(
            ["aws", "ecr", "get-login-password", "--region", region],
            timeout=30,
        )
        if rc != 0:
            return False, f"ECR 토큰 발급 실패: {err}"

        # docker login
        try:
            proc = subprocess.run(
                ["docker", "login", "--username", "AWS", "--password-stdin", registry],
                input=token, capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                return False, f"docker login 실패: {proc.stderr}"
        except Exception as e:
            return False, f"docker login 예외: {e}"

        # ECR 레포지토리 생성 (이미 있으면 무시)
        self._run(
            ["aws", "ecr", "create-repository", "--repository-name", repo_name,
             "--region", region],
            timeout=15,
        )

        ecr_uri = f"{registry}/{repo_name}:{tag}"
        rc, _, err = self._run(["docker", "tag", local_image, ecr_uri], timeout=30)
        if rc != 0:
            return False, f"docker tag 실패: {err}"

        rc, out, err = self._run(["docker", "push", ecr_uri], timeout=_ECR_PUSH_TIMEOUT)
        if rc != 0:
            return False, f"docker push 실패: {err or out}"

        logger.info(f"[ecs_deploy] ECR push 완료: {ecr_uri}")
        return True, ecr_uri

    # ── Step 3: Task Definition 등록 ────────────────────────────────

    def register_task_definition(
        self,
        config: ECSDeployConfig,
        ecr_image_uri: str,
        family: str,
        execution_role_arn: str = "",
        task_role_arn: str = "",
    ) -> tuple[bool, str]:
        """
        현재 서비스의 Task Definition 을 기반으로 이미지만 교체한 새 revision 등록.
        기존 Task Definition 이 없으면 최소 스펙으로 새로 생성.
        반환: (success, task_definition_arn)
        """
        import boto3
        ecs = self._boto3_ecs(config)

        # 현재 서비스의 Task Definition 조회
        base_container_defs = None
        try:
            svc = ecs.describe_services(
                cluster=config.ecs_cluster,
                services=[config.ecs_service],
            )["services"]
            if svc:
                current_td_arn = svc[0].get("taskDefinition", "")
                if current_td_arn:
                    td = ecs.describe_task_definition(taskDefinition=current_td_arn)
                    base_container_defs = td["taskDefinition"]["containerDefinitions"]
        except Exception as e:
            logger.warning(f"[ecs_deploy] 기존 Task Definition 조회 실패: {e}")

        # 컨테이너 정의: 기존 기반으로 이미지만 교체, 없으면 최소 스펙 생성
        if base_container_defs:
            container_defs = []
            for cd in base_container_defs:
                new_cd = dict(cd)
                if cd["name"] == config.container_name:
                    new_cd["image"] = ecr_image_uri
                container_defs.append(new_cd)
        else:
            container_defs = [{
                "name": config.container_name,
                "image": ecr_image_uri,
                "portMappings": [
                    {"containerPort": config.container_port, "protocol": "tcp"}
                ],
                "environment": config.env_vars,
                "essential": True,
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": f"/ecs/{family}",
                        "awslogs-region": config.aws_region,
                        "awslogs-stream-prefix": "ecs",
                        "awslogs-create-group": "true",
                    },
                },
            }]

        kwargs: dict = dict(
            family=family,
            networkMode="awsvpc",
            requiresCompatibilities=["FARGATE"],
            cpu=config.cpu,
            memory=config.memory,
            containerDefinitions=container_defs,
        )
        if execution_role_arn:
            kwargs["executionRoleArn"] = execution_role_arn
        if task_role_arn:
            kwargs["taskRoleArn"] = task_role_arn

        try:
            resp = ecs.register_task_definition(**kwargs)
            td_arn = resp["taskDefinition"]["taskDefinitionArn"]
            logger.info(f"[ecs_deploy] Task Definition 등록: {td_arn}")
            return True, td_arn
        except Exception as e:
            return False, f"Task Definition 등록 실패: {e}"

    # ── Step 4: ECS Service 업데이트 ────────────────────────────────

    def update_service(
        self,
        config: ECSDeployConfig,
        task_definition_arn: str,
    ) -> tuple[bool, str]:
        """
        ECS Service 를 새 Task Definition 으로 업데이트.
        update-service --force-new-deployment 호출.
        반환: (success, error_message)
        """
        ecs = self._boto3_ecs(config)
        try:
            ecs.update_service(
                cluster=config.ecs_cluster,
                service=config.ecs_service,
                taskDefinition=task_definition_arn,
                forceNewDeployment=True,
            )
            logger.info(f"[ecs_deploy] Service 업데이트 요청 완료")
            return True, ""
        except Exception as e:
            return False, f"ECS Service 업데이트 실패: {e}"

    # ── Step 5: 배포 상태 폴링 + Circuit Breaker ────────────────────

    def wait_for_deployment(
        self,
        config: ECSDeployConfig,
        log_fn=None,
    ) -> tuple[bool, str]:
        """
        ECS 배포 완료까지 폴링.
        Circuit Breaker: 5분 내 Health Check 실패율 50% 초과 시 자동 중단.

        반환: (success, error_message)
        """
        ecs = self._boto3_ecs(config)
        deadline = time.time() + _DEPLOY_POLL_TIMEOUT

        # Circuit Breaker 상태
        health_events: list[tuple[float, bool]] = []  # (timestamp, is_healthy)

        def _log(msg: str) -> None:
            logger.info(msg)
            if log_fn:
                log_fn(msg)

        _log(f"[ECS] 배포 상태 폴링 시작 (최대 {_DEPLOY_POLL_TIMEOUT // 60}분)...")

        while time.time() < deadline:
            time.sleep(_DEPLOY_POLL_INTERVAL)
            try:
                svc = ecs.describe_services(
                    cluster=config.ecs_cluster,
                    services=[config.ecs_service],
                )["services"][0]
            except Exception as e:
                _log(f"[ECS] 상태 조회 실패: {e}")
                continue

            deployments = svc.get("deployments", [])
            primary = next(
                (d for d in deployments if d["status"] == "PRIMARY"), None
            )
            if not primary:
                continue

            running   = primary.get("runningCount", 0)
            desired   = primary.get("desiredCount", 0)
            failed    = primary.get("failedTasks", 0)
            rollout   = primary.get("rolloutState", "")

            now = time.time()
            is_healthy = failed == 0 and running > 0
            health_events.append((now, is_healthy))

            # Circuit Breaker: 5분 창 내 이벤트만 유지
            health_events = [
                (t, h) for t, h in health_events
                if now - t <= _CB_WINDOW_SECONDS
            ]
            if len(health_events) >= 4:
                failure_rate = sum(1 for _, h in health_events if not h) / len(health_events)
                if failure_rate > _CB_FAILURE_THRESHOLD:
                    return False, (
                        f"Circuit Breaker 동작: 최근 5분 내 Health Check 실패율 "
                        f"{failure_rate:.0%} (임계값 {_CB_FAILURE_THRESHOLD:.0%} 초과). "
                        "배포를 자동 중단합니다."
                    )

            _log(
                f"[ECS] running={running}/{desired} failed={failed} "
                f"rollout={rollout}"
            )

            if rollout == "COMPLETED" and running == desired and failed == 0:
                _log("[ECS] 배포 성공!")
                return True, ""

            if rollout == "FAILED":
                return False, f"ECS 배포 실패 (rolloutState=FAILED, failedTasks={failed})"

        return False, f"배포 타임아웃 ({_DEPLOY_POLL_TIMEOUT // 60}분 초과)"

    # ── Step 6: Rollback Proposal 생성 ──────────────────────────────

    def make_rollback_proposal(
        self,
        config: ECSDeployConfig,
        failed_td_arn: str,
        prev_td_arn: str,
        reason: str,
    ) -> dict:
        """
        실패 시 이전 Task Definition 으로 rollback proposal 반환 (Approval Level 3).
        실제 rollback 실행은 승인 후 별도 처리.
        """
        return {
            "type":           "rollback_proposal",
            "approval_level": 3,
            "cluster":        config.ecs_cluster,
            "service":        config.ecs_service,
            "failed_task_definition":   failed_td_arn,
            "rollback_task_definition": prev_td_arn,
            "reason":         reason,
            "created_at":     datetime.now(timezone.utc).isoformat(),
        }

    # ── 전체 파이프라인 ───────────────────────────────────────────────

    def deploy(
        self,
        workspace_path: str,
        image_name: str,
        repo_name: str,
        config: ECSDeployConfig,
        tag: str = "latest",
        family: str = "",
        log_fn=None,
        environment: str = "staging",
        branch: str = "",
        skip_sbom: bool = False,
        skip_opa: bool = False,
    ) -> ECSDeployResult:
        """
        ECS Fargate Rolling Update 전체 파이프라인.

        0. SBOM 생성 (ECR push 후 이미지 스캔) — Q3 Must
        0-b. OPA 게이트 (Trivy critical / SBOM 없는 배포 차단) — Q3 Must
        1. docker build
        2. ECR push
        3. Task Definition 새 revision 등록
        4. ECS Service update-service
        5. CloudWatch 폴링 + Circuit Breaker
        6. 실패 시 rollback proposal 생성

        Args:
            workspace_path: Dockerfile 위치
            image_name:     로컬 이미지명
            repo_name:      ECR 레포지토리명
            config:         ECSDeployConfig
            tag:            이미지 태그
            family:         Task Definition family 이름 (기본값: repo_name)
            log_fn:         로그 콜백 함수
            environment:    배포 환경 (staging / production)
            branch:         현재 Git 브랜치 (OPA 정책 평가용)
            skip_sbom:      SBOM 생성 건너뜀 (테스트용)
            skip_opa:       OPA 게이트 건너뜀 (테스트용)
        """
        logs: list[str] = []
        family = family or repo_name
        image_tag = f"{image_name}:{tag}"
        prev_td_arn = ""

        def _log(msg: str) -> None:
            logs.append(msg)
            if log_fn:
                log_fn(msg)

        # 현재 서비스 Task Definition 저장 (rollback 후보)
        try:
            ecs = self._boto3_ecs(config)
            svc = ecs.describe_services(
                cluster=config.ecs_cluster,
                services=[config.ecs_service],
            )["services"]
            if svc:
                prev_td_arn = svc[0].get("taskDefinition", "")
        except Exception:
            pass

        # Step 1: docker build
        _log(f"[BUILD] {image_tag} 빌드 시작...")
        ok, out = self.build_image(workspace_path, image_tag)
        _log(f"[BUILD] {'완료' if ok else '실패'}: {out[:200]}")
        if not ok:
            return ECSDeployResult(success=False, error=out, logs=logs)

        # Step 2: ECR push
        _log(f"[ECR] push 시작... ({config.ecr_registry}/{repo_name}:{tag})")
        ok, ecr_uri = self.ecr_push(
            image_tag, config.ecr_registry, repo_name, tag, config.aws_region
        )
        _log(f"[ECR] push {'완료' if ok else '실패'}: {ecr_uri[:120]}")
        if not ok:
            return ECSDeployResult(success=False, error=ecr_uri, logs=logs)

        # Step 2-b: SBOM 생성 (Q3 Must) ────────────────────────────────
        sbom_summary: Optional[dict] = None
        if not skip_sbom:
            _log(f"[SBOM] Syft 스캔 시작... ({ecr_uri})")
            try:
                from sbom_agent import get_sbom_agent
                sbom_result = get_sbom_agent().generate(ecr_uri, tag=tag, log_fn=_log)
                if sbom_result.success:
                    sbom_summary = sbom_result.to_summary()
                    _log(
                        f"[SBOM] 완료 — 패키지 {sbom_result.package_count}개, "
                        f"hash={sbom_result.sbom_hash[:16]}..."
                    )
                else:
                    _log(f"[SBOM] 생성 실패: {sbom_result.error}")
            except Exception as e:
                _log(f"[SBOM] SBOM 에이전트 오류: {e}")
        else:
            _log("[SBOM] 건너뜀 (skip_sbom=True)")

        # Step 2-c: OPA 게이트 (Q3 Must) ────────────────────────────────
        if not skip_opa:
            _log("[OPA] 배포 게이트 평가 중...")
            try:
                from opa_gate import get_opa_gate
                opa_result = get_opa_gate().evaluate_ecs_deploy(
                    image_uri=ecr_uri,
                    sbom_result=sbom_summary,
                    environment=environment,
                    branch=branch,
                )
                _log(f"[OPA] 결과: {opa_result.decision.value} — {opa_result.reason}")
                if opa_result.blocked:
                    error_msg = (
                        f"OPA 게이트 차단: {opa_result.reason}"
                        + (f"\n수정 제안: {opa_result.fix_suggestion}" if opa_result.fix_suggestion else "")
                    )
                    return ECSDeployResult(
                        success=False,
                        error=error_msg,
                        image_uri=ecr_uri,
                        logs=logs,
                    )
            except Exception as e:
                # OPA 게이트 자체 오류 → fail-closed (Level 3)
                error_msg = f"OPA 게이트 오류 (fail-closed): {e}"
                _log(f"[OPA] {error_msg}")
                return ECSDeployResult(
                    success=False,
                    error=error_msg,
                    image_uri=ecr_uri,
                    logs=logs,
                )
        else:
            _log("[OPA] 건너뜀 (skip_opa=True)")

        # Step 3: Task Definition 등록
        _log(f"[ECS] Task Definition 등록 중... (family={family})")
        ok, td_arn = self.register_task_definition(config, ecr_uri, family)
        _log(f"[ECS] Task Definition {'등록 완료' if ok else '실패'}: {td_arn[:80]}")
        if not ok:
            return ECSDeployResult(
                success=False, error=td_arn, logs=logs, image_uri=ecr_uri
            )

        # Step 4: Service 업데이트
        _log(f"[ECS] Service 업데이트 중... ({config.ecs_cluster}/{config.ecs_service})")
        ok, err = self.update_service(config, td_arn)
        _log(f"[ECS] Service 업데이트 {'완료' if ok else '실패'}")
        if not ok:
            return ECSDeployResult(
                success=False, error=err, logs=logs,
                image_uri=ecr_uri, task_definition_arn=td_arn,
            )

        # Step 5: 배포 폴링 + Circuit Breaker
        ok, err = self.wait_for_deployment(config, log_fn=_log)

        if ok:
            deployed_at = datetime.utcnow().isoformat() + "Z"
            _log("[SUCCESS] ECS 배포 완료")
            return ECSDeployResult(
                success=True,
                image_uri=ecr_uri,
                task_definition_arn=td_arn,
                prev_task_def_arn=prev_td_arn,
                logs=logs,
                deployed_at=deployed_at,
            )

        # Step 6: 실패 → rollback proposal
        _log(f"[FAIL] 배포 실패: {err}")
        _log("[ROLLBACK] 이전 Task Definition 으로 rollback proposal 생성 (Approval Level 3)...")
        return ECSDeployResult(
            success=False,
            error=err,
            image_uri=ecr_uri,
            task_definition_arn=td_arn,
            prev_task_def_arn=prev_td_arn,
            rollback_required=True,
            logs=logs,
        )


# ── Cloud Preflight 체크 (설계서 §Q3) ────────────────────────────────

def check_ecs_preflight(config: ECSDeployConfig) -> dict:
    """
    ECS 배포 사전 조건 확인 (read-only IAM 사용).
    반환: { ready: bool, issues: [...], warnings: [...] }
    """
    import boto3, shutil

    issues: list[str] = []
    warnings: list[str] = []

    # CLI 도구 확인
    if not shutil.which("docker"):
        issues.append("Docker 미설치")
    if not shutil.which("aws"):
        issues.append("AWS CLI 미설치")

    # AWS 자격증명 확인
    try:
        boto3.client("sts", region_name=config.aws_region).get_caller_identity()
    except Exception as e:
        issues.append(f"AWS 자격증명 오류: {e}")
        return {"ready": False, "issues": issues, "warnings": warnings}

    # ECS 클러스터 확인
    try:
        ecs = boto3.client("ecs", region_name=config.aws_region)
        clusters = ecs.describe_clusters(clusters=[config.ecs_cluster])["clusters"]
        if not clusters or clusters[0]["status"] != "ACTIVE":
            issues.append(f"ECS 클러스터 없음 또는 비활성: {config.ecs_cluster}")
    except Exception as e:
        issues.append(f"ECS 클러스터 확인 실패: {e}")

    # ECS 서비스 확인
    try:
        services = ecs.describe_services(
            cluster=config.ecs_cluster,
            services=[config.ecs_service],
        )["services"]
        if not services or services[0]["status"] != "ACTIVE":
            issues.append(f"ECS 서비스 없음 또는 비활성: {config.ecs_service}")
    except Exception as e:
        issues.append(f"ECS 서비스 확인 실패: {e}")

    # ECR 레지스트리 확인
    try:
        ecr = boto3.client("ecr", region_name=config.aws_region)
        ecr.describe_repositories()
    except Exception as e:
        warnings.append(f"ECR 접근 확인 필요: {e}")

    return {
        "ready":    len(issues) == 0,
        "issues":   issues,
        "warnings": warnings,
    }


# ── 싱글턴 ────────────────────────────────────────────────────────────

_instance: Optional[ECSDeployAgent] = None


def get_ecs_deploy_agent() -> ECSDeployAgent:
    global _instance
    if _instance is None:
        _instance = ECSDeployAgent()
    return _instance


__all__ = [
    "ECSDeployAgent",
    "ECSDeployConfig",
    "ECSDeployResult",
    "get_ecs_deploy_agent",
    "check_ecs_preflight",
]
