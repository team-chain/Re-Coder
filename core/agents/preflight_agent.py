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

from core import aws_policy
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


#: "조회했더니 그 리소스가 없더라"로 봐야 하는 AWS 오류 코드.
#:
#: 빈 계정에서 `describe_services` 는 빈 목록을 돌려주지 않고
#: **`ClusterNotFoundException` 을 던진다.** 이걸 일반 오류로 처리하면
#: `_boto3_error` 가 severity="error" 로 떨어뜨리고, 그러면
#: `missing_severity="warning"` 이 아예 적용되지 않는다. 결과적으로
#: 빈 계정의 첫 배포가 preflight 에서 막힌다 — 클러스터를 만들어 주는
#: 코드가 바로 다음 단계에 있는데도.
_NOT_FOUND_CODES = frozenset({
    "ClusterNotFoundException",
    "ServiceNotFoundException",
    "ServiceNotActiveException",
})


def _is_not_found(exc: Exception) -> bool:
    """리소스가 아직 없다는 뜻의 예외인가."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict) and error.get("Code") in _NOT_FOUND_CODES:
            return True
    # botocore 가 없거나 예외 타입이 다른 환경을 위한 보조 판정
    return any(code in str(exc) for code in _NOT_FOUND_CODES)


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
        will_provision: bool = False,
    ) -> PreflightReport:
        """
        Preflight 전체 실행.
        CloudPreflight를 통과하지 못한 환경은 배포 측정에서 제외한다 (설계서 §Q3 DoD).

        `will_provision=True` 는 "배포 파이프라인이 없는 리소스를 직접 만든다"는
        뜻이다. 이때 클러스터·서비스가 **아직 없는 것은 실패가 아니다** —
        그게 정상 출발 상태다. 예전에는 이 구분이 없어서, 자동 생성 기능을
        preflight 가 막고 있었다: 빈 계정에서 첫 배포가 "클러스터를 찾을 수
        없습니다"로 중단되는데, 정작 그 클러스터를 만들어 주는 코드는
        preflight 바로 다음 단계에 있었다.
        """
        report = PreflightReport(region=region, cluster=cluster, service=service)

        # 우리가 만들어 줄 리소스는 없어도 경고로만 남긴다.
        creatable = "warning" if will_provision else "error"
        checks = [
            await self._check_ecs_cluster(cluster, region, missing_severity=creatable),
            await self._check_ecs_service(
                cluster, service, region, missing_severity=creatable
            ),
            # 역할 이름은 권한표와 **같은 출처**에서 가져온다. 여기 박아두면
            # 학교 계정처럼 역할 이름이 다른 환경에서 없는 역할을 찾게 된다.
            await self._check_iam_role(
                # GetRole 은 **경로 없는** 이름을 받는다. 정책 ARN 은 경로를
                # 포함해야 하므로 둘을 섞으면 한쪽이 틀린다.
                aws_policy.role_short_name(aws_policy.configured_execution_role()),
                region,
            ),
            await self._check_log_group(
                log_group or f"/ecs/{task_definition_family}", region
            ),
            # NOTE: 로그 그룹 이름의 규칙(`/ecs/{family}`)은 여기서 다시
            # 계산하지 않는다 — 호출자가 만들어 넘긴다. 같은 값을 두 곳에서
            # 계산하면 한쪽만 바뀌었을 때 조용히 어긋난다.
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

    async def _check_ecs_cluster(
        self, cluster: str, region: str, *, missing_severity: str = "error"
    ) -> PreflightCheck:
        name = "ECS Cluster 존재 확인"
        try:
            client = self._ecs_client(region)
            resp = client.describe_clusters(clusters=[cluster])
            clusters = resp.get("clusters", [])
            active = [c for c in clusters if c.get("status") == "ACTIVE"]
            if active:
                return PreflightCheck(name=name, passed=True, detail=f"클러스터 '{cluster}' ACTIVE", severity="error")
            return self._missing(name, cluster, missing_severity, "클러스터",
                                 f"`aws ecs create-cluster --cluster-name {cluster} "
                                 f"--region {region}` 를 실행하세요")
        except Exception as e:
            if _is_not_found(e):
                return self._missing(name, cluster, missing_severity, "클러스터",
                                     f"`aws ecs create-cluster --cluster-name "
                                     f"{cluster} --region {region}` 를 실행하세요")
            return self._boto3_error(name, e)

    async def _check_ecs_service(
        self, cluster: str, service: str, region: str, *,
        missing_severity: str = "error"
    ) -> PreflightCheck:
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
            return self._missing(name, service, missing_severity, "서비스",
                                 "ECS 콘솔에서 서비스를 먼저 생성하세요")
        except Exception as e:
            # 빈 계정에서는 클러스터가 없어 ClusterNotFoundException 이 난다.
            # 그건 "아직 안 만들어졌다"이지 점검 실패가 아니다.
            if _is_not_found(e):
                return self._missing(name, service, missing_severity, "서비스",
                                     "ECS 콘솔에서 서비스를 먼저 생성하세요")
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
    def _missing(
        name: str, resource: str, severity: str, kind: str, manual_guide: str
    ) -> PreflightCheck:
        """아직 없는 리소스에 대한 점검 결과.

        `severity` 가 warning 이면 배포를 막지 않는다 — 파이프라인이
        만들어 줄 것이기 때문이다.
        """
        return PreflightCheck(
            name=name, passed=False, severity=severity,
            detail=f"{kind} '{resource}'를 찾을 수 없습니다",
            fix_guide=("배포 중에 자동으로 생성됩니다"
                       if severity == "warning" else manual_guide),
        )

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
