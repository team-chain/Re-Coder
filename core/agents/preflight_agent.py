"""
Local Core — Q3: Cloud Preflight Assistant

설계서 §Q3:
- read-only IAM만 사용
- ECS/ECR/ALB/CloudWatch 리소스 사전 점검
- 미충족 리소스에 대한 안내는 "실행 명령"이 아닌 "가이드"로 표시
- IAM, Security Group, Public ALB 관련 명령은 Level 4 경고 표시
- 0.0.0.0/0 오픈 명령은 위험 경고 기본 표시
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.schemas import PreflightCheck, PreflightReport

logger = logging.getLogger(__name__)

# read-only IAM 권한만 사용 (설계서 §Q3)
_REQUIRED_READ_ACTIONS = [
    "ecr:DescribeRepositories",
    "ecs:DescribeClusters",
    "ecs:DescribeServices",
    "iam:GetRole",
    "logs:DescribeLogGroups",
    "elbv2:DescribeLoadBalancers",
    "elbv2:DescribeTargetGroups",
]


class PreflightAgent:
    """
    ECS 배포 전 AWS 리소스 사전 점검 에이전트.
    모든 API 호출은 read-only이며 쓰기 권한을 요구하지 않는다.
    """

    def __init__(self) -> None:
        try:
            import boto3
            self._boto3 = boto3
        except ImportError:
            self._boto3 = None
            logger.warning("boto3 not installed — preflight will run in mock mode")

    async def run(
        self,
        cluster: str,
        service: str,
        region: str,
        task_definition_family: str,
        ecr_repo: Optional[str] = None,
        log_group: Optional[str] = None,
    ) -> PreflightReport:
        """
        Preflight 전체 실행.
        CloudPreflight를 통과하지 못한 환경은 배포 측정에서 제외한다 (설계서 §Q3 DoD).
        """
        report = PreflightReport(region=region, cluster=cluster, service=service)

        checks = [
            await self._check_ecs_cluster(cluster, region),
            await self._check_ecs_service(cluster, service, region),
            await self._check_iam_role(f"ecsTaskExecutionRole", region),
            await self._check_log_group(
                log_group or f"/ecs/{task_definition_family}", region
            ),
        ]

        if ecr_repo:
            checks.append(await self._check_ecr_repo(ecr_repo, region))

        report.checks = checks
        report.compute_pass()
        logger.info(
            "Preflight %s: cluster=%s service=%s passed=%s",
            "PASS" if report.passed else "FAIL", cluster, service, report.passed,
        )
        return report

    # ------------------------------------------------------------------
    # 개별 체크
    # ------------------------------------------------------------------

    async def _check_ecs_cluster(self, cluster: str, region: str) -> PreflightCheck:
        name = "ECS Cluster 존재 확인"
        try:
            client = self._ecs_client(region)
            resp = client.describe_clusters(clusters=[cluster])
            clusters = resp.get("clusters", [])
            active = [c for c in clusters if c.get("status") == "ACTIVE"]
            if active:
                return PreflightCheck(name=name, passed=True, detail=f"클러스터 '{cluster}' ACTIVE", severity="error")
            return PreflightCheck(
                name=name, passed=False, severity="error",
                detail=f"클러스터 '{cluster}'를 찾을 수 없습니다",
                fix_guide=f"`aws ecs create-cluster --cluster-name {cluster} --region {region}` 를 실행하세요",
            )
        except Exception as e:
            return self._boto3_error(name, e)

    async def _check_ecs_service(self, cluster: str, service: str, region: str) -> PreflightCheck:
        name = "ECS Service 존재 확인"
        try:
            client = self._ecs_client(region)
            resp = client.describe_services(cluster=cluster, services=[service])
            services = resp.get("services", [])
            active = [s for s in services if s.get("status") == "ACTIVE"]
            if active:
                running = active[0].get("runningCount", 0)
                desired = active[0].get("desiredCount", 0)
                return PreflightCheck(
                    name=name, passed=True, severity="error",
                    detail=f"서비스 '{service}' ACTIVE (running={running}, desired={desired})",
                )
            return PreflightCheck(
                name=name, passed=False, severity="error",
                detail=f"서비스 '{service}'를 찾을 수 없습니다",
                fix_guide="ECS 콘솔에서 서비스를 먼저 생성하세요",
            )
        except Exception as e:
            return self._boto3_error(name, e)

    async def _check_iam_role(self, role_name: str, region: str) -> PreflightCheck:
        name = f"IAM Role '{role_name}' 존재 확인"
        try:
            client = self._iam_client()
            client.get_role(RoleName=role_name)
            return PreflightCheck(name=name, passed=True, severity="error",
                                  detail=f"IAM Role '{role_name}' 존재 확인됨")
        except Exception as e:
            err = str(e)
            if "NoSuchEntity" in err:
                return PreflightCheck(
                    name=name, passed=False, severity="error",
                    detail=f"IAM Role '{role_name}'이 없습니다",
                    fix_guide=(
                        "AWS 콘솔 → IAM → Roles → Create Role → "
                        "ECS Task Execution 정책을 붙여주세요 (Level 4 권한 필요)"
                    ),
                )
            return self._boto3_error(name, e)

    async def _check_ecr_repo(self, repo: str, region: str) -> PreflightCheck:
        name = f"ECR Repository '{repo}' 존재 확인"
        try:
            client = self._ecr_client(region)
            client.describe_repositories(repositoryNames=[repo])
            return PreflightCheck(name=name, passed=True, severity="error",
                                  detail=f"ECR repo '{repo}' 존재 확인됨")
        except Exception as e:
            err = str(e)
            if "RepositoryNotFoundException" in err:
                return PreflightCheck(
                    name=name, passed=False, severity="error",
                    detail=f"ECR repo '{repo}'가 없습니다",
                    fix_guide=f"`aws ecr create-repository --repository-name {repo} --region {region}`",
                )
            return self._boto3_error(name, e)

    async def _check_log_group(self, log_group: str, region: str) -> PreflightCheck:
        name = f"CloudWatch Log Group '{log_group}' 확인"
        try:
            client = self._logs_client(region)
            resp = client.describe_log_groups(logGroupNamePrefix=log_group)
            groups = resp.get("logGroups", [])
            if any(g["logGroupName"] == log_group for g in groups):
                return PreflightCheck(name=name, passed=True, severity="warning",
                                      detail=f"Log Group '{log_group}' 존재 확인됨")
            return PreflightCheck(
                name=name, passed=False, severity="warning",
                detail=f"Log Group '{log_group}'이 없습니다 (ECS가 자동 생성할 수 있음)",
                fix_guide=f"`aws logs create-log-group --log-group-name {log_group} --region {region}`",
            )
        except Exception as e:
            return self._boto3_error(name, e, severity="warning")

    # ------------------------------------------------------------------
    # boto3 클라이언트 헬퍼
    # ------------------------------------------------------------------

    def _ecs_client(self, region: str):
        if self._boto3 is None:
            raise RuntimeError("boto3 not installed")
        return self._boto3.client("ecs", region_name=region)

    def _ecr_client(self, region: str):
        if self._boto3 is None:
            raise RuntimeError("boto3 not installed")
        return self._boto3.client("ecr", region_name=region)

    def _iam_client(self):
        if self._boto3 is None:
            raise RuntimeError("boto3 not installed")
        return self._boto3.client("iam")

    def _logs_client(self, region: str):
        if self._boto3 is None:
            raise RuntimeError("boto3 not installed")
        return self._boto3.client("logs", region_name=region)

    @staticmethod
    def _boto3_error(name: str, exc: Exception, severity: str = "error") -> PreflightCheck:
        msg = str(exc)
        # 자격증명 오류는 명확하게 안내
        if "credentials" in msg.lower() or "NoCredentials" in msg:
            return PreflightCheck(
                name=name, passed=False, severity=severity,
                detail="AWS 자격증명이 설정되지 않았습니다",
                fix_guide="~/.aws/credentials 또는 환경변수 AWS_ACCESS_KEY_ID를 확인하세요",
            )
        return PreflightCheck(name=name, passed=False, severity=severity,
                              detail=f"AWS API 오류: {msg}")
