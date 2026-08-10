"""
ReCoder Core — ECS Fargate 기동에 필요한 AWS 리소스 확보(ensure)

FR-05-04. 이 모듈이 담당하는 것은 카드 「뭘 만들면 되나요」 3·4단계다:
"ECS 클러스터 + Fargate 서비스 생성/갱신", "공개 접근 확인 → URL 반환".

설계 원칙 세 가지:

1. **모든 함수는 멱등하다.**
   두 번 불러도 같은 결과가 나오고 "이미 있음" 오류로 죽지 않는다.
   AWS Academy Learner Lab 은 세션이 4시간마다 끊기지만 만들어둔 리소스는
   남는다. 그래서 두 번째 실행이 깨지면 그건 완성이 아니다.

2. **boto3 클라이언트를 인자로 받는다.**
   모듈이 스스로 자격증명을 만들지 않으므로 moto 로 그대로 테스트된다.
   자격증명 수명 관리는 호출자(ECSAgent) 책임이다.

3. **실패는 사람이 읽을 수 있는 문장으로 바꾼다.**
   카드 DoD 3번이 "실패 시 사람이 읽을 수 있는 에러 메시지"다.
   botocore 의 원문은 `InfraError.detail` 에 보존하고, `str(exc)` 는
   무엇을 해야 하는지가 담긴 한국어 문장이 되게 한다.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# CloudWatch Logs 가 받아주는 보관 기간(일). 목록에 없는 값을 주면
# InvalidParameterException 이 난다 — 우리가 먼저 걸러서 알려준다.
VALID_RETENTION_DAYS = (
    1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096,
    1827, 2192, 2557, 2922, 3288, 3653,
)

#: 개발용 기본값. 랩 예산($50)을 갉아먹지 않도록 짧게 잡는다.
DEFAULT_LOG_RETENTION_DAYS = 7

#: ECR 에 남겨둘 최근 이미지 개수. 배포할 때마다 이미지가 쌓이는데
#: GB 당 월 $0.10 이라 방치하면 조용히 예산을 먹는다.
DEFAULT_ECR_KEEP_LAST = 5


# ---------------------------------------------------------------------------
# 오류 타입
# ---------------------------------------------------------------------------


class InfraError(RuntimeError):
    """사람이 읽을 수 있는 메시지를 가진 인프라 오류.

    `str(exc)` 는 사용자에게 그대로 보여줄 수 있는 문장이고,
    `detail` 에 AWS 원문을 보존한다. 원문을 버리면 나중에 디버깅이 막힌다.
    """

    def __init__(self, message: str, *, detail: str = "", remedy: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.remedy = remedy

    def human(self) -> str:
        parts = [self.message]
        if self.remedy:
            parts.append(f"해결 방법: {self.remedy}")
        return " ".join(parts)


class NetworkNotFound(InfraError):
    """기본 VPC 또는 인터넷 연결된 서브넷을 찾지 못했다."""


# ---------------------------------------------------------------------------
# botocore 오류 헬퍼
# ---------------------------------------------------------------------------


def error_code(exc: BaseException) -> str:
    """ClientError 의 AWS 오류 코드를 꺼낸다. 아니면 빈 문자열."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    return str(error.get("Code") or "")


def error_message(exc: BaseException) -> str:
    """ClientError 의 AWS 오류 메시지를 꺼낸다. 아니면 str(exc)."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict) and error.get("Message"):
            return str(error["Message"])
    return str(exc)


#: AWS Academy Learner Lab Readme(2025-06-24)에 명시된 알려진 현상:
#: "If you see a message 'the ECS service linked role could not be assumed'
#:  choose the back button and then try again. This sometimes happens if the
#:  service linked role does not yet exist in your account."
#:
#: 사람에게는 "다시 눌러라"지만 코드에는 재시도 로직이어야 한다.
#: 이게 없으면 계정의 **첫 배포**가 무작위로 실패한다.
_SERVICE_LINKED_ROLE_PATTERN = re.compile(
    r"service[- ]linked role", re.IGNORECASE
)


def is_service_linked_role_race(exc: BaseException) -> bool:
    """ECS 서비스 연결 롤이 아직 준비되지 않아 생긴 일시적 실패인가."""
    if error_code(exc) not in ("InvalidParameterException", "ServerException", ""):
        return False
    return bool(_SERVICE_LINKED_ROLE_PATTERN.search(error_message(exc)))


def retry_on_transient(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    predicate: Callable[[BaseException], bool] = is_service_linked_role_race,
) -> T:
    """`predicate` 가 참인 예외에 대해서만 지수 백오프로 재시도한다.

    predicate 가 거짓인 예외는 **즉시 올린다.** 모든 예외를 재시도하면
    권한 오류 같은 영구 실패에서 네 번씩 기다리게 되고, 사용자는
    잘못된 원인을 보게 된다.
    """
    if attempts < 1:
        raise ValueError("attempts 는 1 이상이어야 한다")
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - predicate 로 걸러 다시 올린다
            if not predicate(exc):
                raise
            last = exc
            if i == attempts - 1:
                break
            delay = base_delay * (2**i)
            logger.warning(
                "일시적 실패로 재시도합니다 (%d/%d, %.1fs 후): %s",
                i + 1, attempts, delay, error_message(exc),
            )
            sleep(delay)
    if last is None:  # pragma: no cover - attempts >= 1 이므로 도달 불가
        raise InfraError("재시도 루프가 예외 없이 종료됐습니다.")
    raise InfraError(
        "ECS 서비스 연결 역할이 아직 준비되지 않아 서비스를 만들지 못했습니다.",
        detail=error_message(last),
        remedy="AWS 계정에서 ECS 를 처음 쓸 때 나는 현상입니다. "
               "잠시 후 배포를 다시 실행하면 대부분 해결됩니다.",
    ) from last


# ---------------------------------------------------------------------------
# 클러스터
# ---------------------------------------------------------------------------


def ensure_cluster(ecs: Any, name: str) -> str:
    """ECS 클러스터를 확보하고 ARN 을 돌려준다. 있으면 그대로 쓴다.

    빈 클러스터는 요금이 없다 — 지우고 다시 만들 이유가 없다.
    """
    try:
        resp = ecs.describe_clusters(clusters=[name])
    except Exception as exc:  # noqa: BLE001
        raise InfraError(
            f"ECS 클러스터 '{name}' 를 조회하지 못했습니다.",
            detail=error_message(exc),
            remedy="AWS 자격증명이 만료되지 않았는지, 리전이 맞는지 확인하세요.",
        ) from exc

    for cluster in resp.get("clusters", []):
        # INACTIVE 는 삭제된 클러스터의 잔해다. 재사용하면 안 된다.
        if cluster.get("status") == "ACTIVE":
            logger.info("ECS 클러스터 재사용: %s", name)
            return str(cluster["clusterArn"])

    try:
        created = ecs.create_cluster(clusterName=name)
    except Exception as exc:  # noqa: BLE001
        raise InfraError(
            f"ECS 클러스터 '{name}' 를 만들지 못했습니다.",
            detail=error_message(exc),
            remedy="권한표의 ecs:CreateCluster 가 부여됐는지 확인하세요.",
        ) from exc
    logger.info("ECS 클러스터 생성: %s", name)
    return str(created["cluster"]["clusterArn"])


# ---------------------------------------------------------------------------
# CloudWatch 로그 그룹
# ---------------------------------------------------------------------------


def ensure_log_group(
    logs: Any,
    name: str,
    *,
    retention_days: Optional[int] = DEFAULT_LOG_RETENTION_DAYS,
) -> str:
    """로그 그룹을 확보하고 보관 기간을 건다.

    보관 기간은 그룹이 이미 있어도 **매번 다시 건다.** 보관 기간 없이
    만들어진 기존 그룹은 로그가 영원히 쌓이므로, 여기서 고쳐준다.
    """
    if retention_days is not None and retention_days not in VALID_RETENTION_DAYS:
        raise InfraError(
            f"로그 보관 기간 {retention_days}일은 CloudWatch 가 받지 않는 값입니다.",
            remedy=f"허용값 중 하나를 쓰세요: {', '.join(map(str, VALID_RETENTION_DAYS))}",
        )

    try:
        logs.create_log_group(logGroupName=name)
        logger.info("로그 그룹 생성: %s", name)
    except Exception as exc:  # noqa: BLE001
        if error_code(exc) != "ResourceAlreadyExistsException":
            raise InfraError(
                f"CloudWatch 로그 그룹 '{name}' 을 만들지 못했습니다.",
                detail=error_message(exc),
                remedy="권한표의 logs:CreateLogGroup 이 부여됐는지 확인하세요.",
            ) from exc
        logger.info("로그 그룹 재사용: %s", name)

    if retention_days is not None:
        try:
            logs.put_retention_policy(
                logGroupName=name, retentionInDays=retention_days
            )
        except Exception as exc:  # noqa: BLE001
            # 보관 기간을 못 걸어도 배포 자체는 진행할 수 있다.
            # 다만 조용히 넘어가면 비용이 새므로 경고는 반드시 남긴다.
            logger.warning(
                "로그 보관 기간을 걸지 못했습니다 (%s일): %s — "
                "로그가 무기한 쌓여 비용이 누적될 수 있습니다.",
                retention_days, error_message(exc),
            )
    return name


# ---------------------------------------------------------------------------
# ECR 리포지토리
# ---------------------------------------------------------------------------


def ecr_lifecycle_policy(keep_last: int) -> dict:
    """최근 `keep_last` 개만 남기는 수명 주기 정책."""
    if keep_last < 1:
        raise InfraError("ECR 보관 개수는 1 이상이어야 합니다.")
    return {
        "rules": [
            {
                "rulePriority": 1,
                "description": f"최근 {keep_last}개 이미지만 보관 (스토리지 비용 억제)",
                "selection": {
                    "tagStatus": "any",
                    "countType": "imageCountMoreThan",
                    "countNumber": keep_last,
                },
                "action": {"type": "expire"},
            }
        ]
    }


def require_cluster(ecs: Any, name: str) -> None:
    """클러스터가 이미 있어야 한다. `provision=False` 전용."""
    try:
        found = ecs.describe_clusters(clusters=[name]).get("clusters", [])
    except Exception as exc:  # noqa: BLE001
        if _is_missing(exc):
            found = []
        else:
            raise InfraError(
                f"클러스터 '{name}' 을 확인하지 못했습니다.",
                detail=error_message(exc),
                remedy="권한표의 ecs:DescribeClusters 를 확인하세요.",
            ) from exc
    if not any(c.get("status") == "ACTIVE" for c in found):
        raise InfraError(
            f"ECS 클러스터 '{name}' 이 없습니다.",
            remedy=f"provision 을 끄면 클러스터를 만들어 주지 않습니다. "
                   f"`aws ecs create-cluster --cluster-name {name}` 로 미리 "
                   f"만들거나 provision 을 켜세요.",
        )


def require_service(ecs: Any, *, cluster: str, service: str) -> None:
    """서비스가 이미 있어야 한다. `provision=False` 전용.

    **여기가 돈이 걸린 자리다.** 로그 그룹·리포지토리는 없어도 공짜지만,
    서비스는 만들어지는 순간 Fargate 태스크가 뜨고 과금이 시작된다.
    이름을 하나 잘못 적으면 사용자의 관리 도구에는 보이지 않는 서비스가
    조용히 생겨 계속 요금을 문다.
    """
    try:
        found = ecs.describe_services(cluster=cluster, services=[service]).get(
            "services", []
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing(exc):
            found = []
        else:
            raise InfraError(
                f"서비스 '{service}' 를 확인하지 못했습니다.",
                detail=error_message(exc),
                remedy="권한표의 ecs:DescribeServices 를 확인하세요.",
            ) from exc
    if not any(s.get("status") == "ACTIVE" for s in found):
        raise InfraError(
            f"ECS 서비스 '{service}' 가 클러스터 '{cluster}' 에 없습니다.",
            remedy="provision 을 끄면 서비스를 만들어 주지 않습니다. 이름이 "
                   "맞는지 확인하거나 provision 을 켜세요. 이름을 잘못 적은 "
                   "채로 만들면 관리 도구에 안 보이는 서비스가 생겨 요금만 "
                   "나갑니다.",
        )


#: "그 리소스가 아직 없다"는 뜻의 AWS 오류 코드.
_MISSING_CODES = frozenset({
    "ClusterNotFoundException", "ServiceNotFoundException",
    "ServiceNotActiveException", "RepositoryNotFoundException",
    "ResourceNotFoundException",
})


def _is_missing(exc: Exception) -> bool:
    return error_code(exc) in _MISSING_CODES


def require_log_group(logs: Any, name: str) -> None:
    """로그 그룹이 이미 있어야 한다. 없으면 **여기서** 막는다.

    `provision=False` 는 "아무것도 만들지 않는다"는 뜻이라 우리가 만들어
    줄 수 없다. 그런데 태스크 정의는 awslogs 드라이버를 쓰고, 로그 그룹이
    없으면 컨테이너가 기동 중에 죽는다(실행 역할에는 `logs:CreateLogGroup`
    이 없다). preflight 는 이걸 **경고**로만 남기므로 통과해 버린다.

    그 결과가 최악이다 — 배포는 시작되고, 태스크는 뜨자마자 죽고,
    안내는 "CloudWatch 로그를 확인하세요"인데 **정작 로깅이 실패 원인이라
    로그가 비어 있다.** 배포 전에 세우는 편이 훨씬 친절하다.
    """
    try:
        found = logs.describe_log_groups(logGroupNamePrefix=name).get("logGroups", [])
    except Exception as exc:  # noqa: BLE001
        raise InfraError(
            f"로그 그룹 '{name}' 을 확인하지 못했습니다.",
            detail=error_message(exc),
            remedy="권한표의 logs:DescribeLogGroups 를 확인하세요.",
        ) from exc

    if not any(str(g.get("logGroupName")) == name for g in found):
        raise InfraError(
            f"로그 그룹 '{name}' 이 없습니다.",
            remedy=f"provision 을 끄면 로그 그룹을 만들어 주지 않습니다. "
                   f"`aws logs create-log-group --log-group-name {name}` 로 "
                   f"미리 만들거나 provision 을 켜세요. 이게 없으면 컨테이너가 "
                   f"기동 중에 죽고, 로그도 안 남아 원인을 찾기 어렵습니다.",
        )


def require_ecr_repository(ecr: Any, name: str) -> str:
    """ECR 리포지토리가 이미 있어야 한다. 리포지토리 URI 를 돌려준다.

    `provision=False` 인데도 리포지토리를 **만들고 있었다.** "아무것도
    만들지 않는다"는 약속과 어긋난다.
    """
    try:
        repos = ecr.describe_repositories(repositoryNames=[name]).get(
            "repositories", []
        )
    except Exception as exc:  # noqa: BLE001
        # **"없다"와 "볼 수 없다"를 구분한다.** 예전에는 둘 다 같은 문구에
        # 같은 대처법("리포지토리를 만드세요")을 냈다. 권한 문제인 사람이
        # 이미 있는 리포지토리를 또 만들러 가게 된다.
        if not _is_missing(exc):
            raise InfraError(
                f"ECR 리포지토리 '{name}' 을 조회하지 못했습니다.",
                detail=error_message(exc),
                remedy="권한표의 ecr:DescribeRepositories 를 확인하세요. "
                       "자격증명이 만료됐을 수도 있습니다.",
            ) from exc
        repos = []
    if not repos:
        raise InfraError(
            f"ECR 리포지토리 '{name}' 이 없습니다.",
            remedy=f"`aws ecr create-repository --repository-name {name}` 로 "
                   f"미리 만들거나 provision 을 켜세요.",
        )
    return str(repos[0]["repositoryUri"])


def ensure_ecr_repository(
    ecr: Any,
    name: str,
    *,
    keep_last: Optional[int] = DEFAULT_ECR_KEEP_LAST,
    scan_on_push: bool = True,
) -> str:
    """ECR 리포지토리를 확보하고 repositoryUri 를 돌려준다."""
    # 수명 주기 정책은 실패해도 배포를 막지 않으려고 경고만 남긴다.
    # 그래서 **잘못된 인자**는 여기서 먼저 걸러야 한다 — 안 그러면
    # 프로그래밍 실수가 조용한 경고 한 줄로 묻힌다.
    if keep_last is not None:
        ecr_lifecycle_policy(keep_last)  # 값 검증용 (결과는 아래에서 다시 만든다)

    repository: dict | None = None
    try:
        repository = ecr.create_repository(
            repositoryName=name,
            imageScanningConfiguration={"scanOnPush": scan_on_push},
        )["repository"]
        logger.info("ECR 리포지토리 생성: %s", name)
    except Exception as exc:  # noqa: BLE001
        if error_code(exc) != "RepositoryAlreadyExistsException":
            raise InfraError(
                f"ECR 리포지토리 '{name}' 를 만들지 못했습니다.",
                detail=error_message(exc),
                remedy="권한표의 ecr:CreateRepository 가 부여됐는지 확인하세요.",
            ) from exc
        try:
            repository = ecr.describe_repositories(repositoryNames=[name])[
                "repositories"
            ][0]
        except Exception as inner:  # noqa: BLE001
            raise InfraError(
                f"ECR 리포지토리 '{name}' 가 이미 있다고 하는데 조회할 수 없습니다.",
                detail=error_message(inner),
                remedy="다른 계정이 같은 이름을 쓰고 있는지 확인하세요.",
            ) from inner
        logger.info("ECR 리포지토리 재사용: %s", name)

    if keep_last is not None:
        try:
            ecr.put_lifecycle_policy(
                repositoryName=name,
                lifecyclePolicyText=json.dumps(ecr_lifecycle_policy(keep_last)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ECR 수명 주기 정책을 걸지 못했습니다: %s — "
                "옛 이미지가 쌓여 스토리지 비용이 누적될 수 있습니다.",
                error_message(exc),
            )

    uri = repository.get("repositoryUri") if repository else None
    if not uri:
        raise InfraError(
            f"ECR 리포지토리 '{name}' 의 주소를 확인하지 못했습니다.",
            remedy="AWS 콘솔에서 리포지토리 상태를 확인하세요.",
        )
    return str(uri)


# ---------------------------------------------------------------------------
# 네트워킹
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NetworkTarget:
    """Fargate 태스크를 띄울 위치."""

    vpc_id: str
    subnet_ids: tuple[str, ...]
    #: 인터넷 게이트웨이로 향하는 경로가 확인된 서브넷인가.
    #: 거짓이면 이미지 pull 이 실패할 수 있다.
    internet_routable: bool = True

    def __post_init__(self) -> None:
        if not self.subnet_ids:
            raise ValueError("subnet_ids 는 비어 있을 수 없다")


def _route_table_has_igw(route_table: dict) -> bool:
    """이 라우트 테이블이 인터넷 게이트웨이로 나가는가."""
    for route in route_table.get("Routes", []):
        gateway = str(route.get("GatewayId") or "")
        if gateway.startswith("igw-"):
            return True
    return False


def internet_routable_subnets(
    ec2: Any, vpc_id: str, subnet_ids: Iterable[str]
) -> tuple[list[str], bool]:
    """인터넷 게이트웨이로 나가는 경로가 있는 서브넷만 골라낸다.

    Fargate 태스크는 `assignPublicIp=ENABLED` 라도 서브넷 라우팅이
    인터넷으로 안 나가면 **ECR 에서 이미지를 못 받아** 기동에 실패한다.
    그때 나오는 오류가 원인을 짐작하기 어려워서, 배포 전에 걸러낸다.

    (랩 Readme 도 SageMaker 안내에서 같은 판별법을 쓴다:
     "Public subnets are the ones that are connected to a route table
      that routes to an 'igw-...' network connection.")

    반환값은 `(서브넷 목록, 확인됨)`. **확인됨이 거짓이면 목록은
    "인터넷으로 나간다고 검증된 것"이 아니라 "확인하지 못해 그대로 돌려준
    것"이다.** 둘을 같은 타입으로 돌려주면 호출자가 구분할 수 없고,
    검증 실패가 검증 성공으로 둔갑한다.
    """
    wanted = list(subnet_ids)
    if not wanted:
        return [], True
    try:
        tables = ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("RouteTables", [])
    except Exception as exc:  # noqa: BLE001
        # 라우팅 확인 실패는 치명적이지 않다 — 진행은 하되 "확인 못 했다"를
        # 호출자에게 반드시 전달한다.
        logger.warning(
            "서브넷 라우팅을 확인하지 못했습니다: %s — 확인 없이 진행합니다.",
            error_message(exc),
        )
        return wanted, False

    main_table: dict | None = None
    explicit: dict[str, dict] = {}
    for table in tables:
        for assoc in table.get("Associations", []):
            if assoc.get("Main"):
                main_table = table
            elif assoc.get("SubnetId"):
                explicit[str(assoc["SubnetId"])] = table

    routable: list[str] = []
    for subnet_id in wanted:
        # 명시적 연결이 없는 서브넷은 VPC 의 메인 라우트 테이블을 따른다.
        table = explicit.get(subnet_id, main_table)
        if table is not None and _route_table_has_igw(table):
            routable.append(subnet_id)
    return routable, True


def choose_routable_subnets(
    ec2: Any, vpc_id: str, subnets: list[dict], *, where: str, strict: bool = True
) -> tuple[list[str], bool]:
    """인터넷으로 나갈 수 있는 서브넷만 골라낸다. `(서브넷, 확인됨)`.

    **기본 VPC 자동 탐색과 사용자 지정 서브넷이 같은 함수를 쓴다.** 예전에
    이 판정을 두 곳에 따로 두었더니 같은 서브넷인데 지정 방식에 따라 결과가
    달랐다: 자동 탐색은 인터넷으로 못 나가는 서브넷을 걸러냈지만, 사용자가
    직접 지정하면 그대로 통과시키고 "확인됨"이라고까지 보고했다. 그러면
    공인/사설 서브넷을 섞어 준 사용자는 태스크 절반이 CannotPullContainerError
    로 죽는 걸 보게 된다 — 이 검사가 막으려던 바로 그 증상이다.

    신호는 둘이다.
      (1) 라우팅 테이블에 igw- 경로가 있는가 — 가장 정확하다.
      (2) 서브넷의 MapPublicIpOnLaunch — 보조 신호.

    (1) 하나만 믿고 하드 실패시키면 라우팅을 우리가 예상한 모양으로 쓰지
    않는 정상 환경까지 막는다. **둘 다 아니라고 할 때만** 막는다.

    `strict=False` 는 그마저도 막지 않는다. 사용자가 서브넷을 **직접 찍어
    준** 경우에 쓴다. PrivateLink 엔드포인트로 ECR 에 닿는 사설 서브넷은
    두 신호가 모두 "인터넷 없음"인데도 멀쩡히 동작한다 — 우리가 모르는
    구성을 사용자가 알고 있을 수 있으므로, 그럴 땐 막지 말고 경고만 한다.
    자동 탐색(strict=True)은 반대다: 우리가 고른 서브넷이 안 되는 것이면
    그건 우리 잘못이므로 배포 전에 막는 편이 친절하다.
    """
    all_ids = [str(s["SubnetId"]) for s in subnets]
    by_igw, verified = internet_routable_subnets(ec2, vpc_id, all_ids)
    by_public_ip = [str(s["SubnetId"]) for s in subnets if s.get("MapPublicIpOnLaunch")]

    if by_igw:
        # verified=False 면 "라우팅을 확인하지 못해 넘겨받은 그대로"라는 뜻이다.
        # 목록은 같아도 의미가 다르므로 verified 를 그대로 들고 나간다.
        return by_igw, verified
    if by_public_ip:
        logger.warning(
            "%s: 라우팅에서 인터넷 게이트웨이 경로를 찾지 못했습니다. "
            "공인 IP 자동 할당(MapPublicIpOnLaunch) 설정을 근거로 진행합니다.",
            where,
        )
        return by_public_ip, False

    if not strict:
        logger.warning(
            "%s: 인터넷으로 나가는 경로를 확인하지 못했습니다. 지정하신 "
            "대로 진행합니다 — PrivateLink 엔드포인트 같은 구성이라면 "
            "정상입니다. 이미지 pull 이 실패하면 이 부분을 먼저 보세요.",
            where,
        )
        return [str(s["SubnetId"]) for s in subnets], False

    raise NetworkNotFound(
        f"{where}에 인터넷으로 나가는 서브넷이 없습니다. 이 상태로는 "
        "컨테이너가 ECR 에서 이미지를 받지 못합니다.",
        remedy="서브넷의 라우팅 테이블에 인터넷 게이트웨이(igw-) 경로가 "
               "있는지, 또는 서브넷에 공인 IP 자동 할당이 켜져 있는지 "
               "확인하세요.",
    )


def discover_default_network(ec2: Any, *, max_subnets: int = 3) -> NetworkTarget:
    """기본 VPC 에서 태스크를 띄울 서브넷을 찾는다.

    VPC 를 **만들지 않는다.** 새로 만들면 NAT 게이트웨이(월 $33)가 딸려와
    학습용 예산을 그것만으로 소진한다. 계정에 이미 있는 기본 VPC 를 쓴다.

    `max_subnets` 로 개수를 제한하는 이유: 서브넷을 여러 AZ 에 걸쳐 주면
    가용성은 오르지만 서비스 설정이 길어지고 테스트가 불안정해진다.
    3개면 AZ 장애 내성으로 충분하다.
    """
    try:
        vpcs = ec2.describe_vpcs(
            Filters=[{"Name": "isDefault", "Values": ["true"]}]
        ).get("Vpcs", [])
    except Exception as exc:  # noqa: BLE001
        raise NetworkNotFound(
            "기본 VPC 를 조회하지 못했습니다.",
            detail=error_message(exc),
            remedy="권한표의 ec2:DescribeVpcs 가 부여됐는지 확인하세요.",
        ) from exc

    if not vpcs:
        raise NetworkNotFound(
            "이 계정·리전에 기본 VPC 가 없습니다.",
            remedy="AWS 콘솔 VPC 화면에서 '기본 VPC 생성'을 한 번 눌러주거나, "
                   "사용할 서브넷을 직접 지정해 주세요.",
        )
    vpc_id = str(vpcs[0]["VpcId"])

    try:
        subnets = ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("Subnets", [])
    except Exception as exc:  # noqa: BLE001
        raise NetworkNotFound(
            f"기본 VPC({vpc_id})의 서브넷을 조회하지 못했습니다.",
            detail=error_message(exc),
            remedy="권한표의 ec2:DescribeSubnets 가 부여됐는지 확인하세요.",
        ) from exc

    if not subnets:
        raise NetworkNotFound(
            f"기본 VPC({vpc_id})에 서브넷이 하나도 없습니다.",
            remedy="AWS 콘솔에서 기본 서브넷을 만들어 주세요.",
        )

    # AZ 이름 순으로 정렬해 실행할 때마다 같은 서브넷이 뽑히게 한다.
    # 무작위로 바뀌면 서비스가 매번 갱신되고 테스트도 흔들린다.
    ordered = sorted(
        subnets, key=lambda s: (str(s.get("AvailabilityZone", "")), str(s["SubnetId"]))
    )

    chosen, verified = choose_routable_subnets(
        ec2, vpc_id, ordered, where=f"기본 VPC({vpc_id})"
    )

    return NetworkTarget(
        vpc_id=vpc_id,
        # 자동 탐색은 개수를 제한한다. 우리가 고른 것이므로 넓게 잡을 이유가
        # 없다. (사용자가 직접 찍어 준 경우는 resolve_subnet_network 참고 —
        # 거기서는 자르지 않는다.)
        subnet_ids=tuple(chosen[:max_subnets]),
        internet_routable=verified,
    )


#: ECS awsvpc 가 서비스 하나에 받아 주는 서브넷 수 상한.
_MAX_AWSVPC_SUBNETS = 16


def resolve_subnet_network(ec2: Any, subnet_ids: Iterable[str]) -> NetworkTarget:
    """사용자가 직접 지정한 서브넷들로부터 VPC 를 알아낸다.

    예전에는 서브넷만 받으면 `vpc_id=""` 로 두고 넘어갔다. 그러면 보안
    그룹을 만들 수 없어서 "서브넷만 지정하면 보안 그룹도 반드시 함께
    지정해야 한다"는, **문서에도 없는 규칙**이 생겼다. 요청 모델은
    보안 그룹을 생략하면 자동 생성한다고 말하고 있는데도 그랬다.

    서브넷을 조회하면 VPC 는 그냥 알 수 있다. 겸사겸사 검증도 한다:
      - 지정한 서브넷이 실제로 존재하는가 (오타를 배포 중이 아니라 여기서 잡는다)
      - 전부 같은 VPC 인가 (ECS awsvpc 는 한 VPC 안의 서브넷만 받는다)
      - 개수가 ECS 상한(16개) 안인가

    인터넷으로 나가지 못하는 서브넷은 **빼고** 넘긴다. 다만 전부 그렇다면
    막지 않는다 — 자동 탐색과 달리 여기서는 사용자가 직접 고른 것이므로,
    우리가 모르는 구성(PrivateLink 등)일 수 있다.

    **개수는 줄이지 않는다.** 자동 탐색은 3개로 자르지만, 사용자가 다섯 개를
    적어 냈다면 다섯 개를 쓰겠다는 뜻이다. 말없이 줄이면 AZ 를 넓게 쓰려던
    의도가 조용히 무너진다.
    """
    wanted = [str(s) for s in subnet_ids]
    if not wanted:
        raise NetworkNotFound("서브넷이 지정되지 않았습니다.")

    try:
        found = ec2.describe_subnets(SubnetIds=wanted).get("Subnets", [])
    except Exception as exc:  # noqa: BLE001
        # 실계정에서는 ID 가 하나만 틀려도 InvalidSubnetID.NotFound 로 여기 온다
        # (부분 목록을 돌려주지 않는다). 그래서 **AWS 원문을 반드시 남긴다** —
        # 어떤 ID 가 문제인지는 거기에만 적혀 있다. 자격증명 만료처럼 서브넷과
        # 무관한 오류도 이 경로로 오므로, 원인을 단정하지 않는다.
        raise NetworkNotFound(
            "지정한 서브넷을 조회하지 못했습니다: " + ", ".join(wanted),
            detail=error_message(exc),
            remedy="AWS 가 알려준 원인을 먼저 보세요. 서브넷 ID 오타나 리전 "
                   "불일치가 흔하고, 자격증명 만료일 수도 있습니다. "
                   "권한표의 ec2:DescribeSubnets 도 함께 확인하세요.",
        ) from exc

    by_id = {str(s["SubnetId"]): s for s in found}
    # moto 등 일부 구현은 없는 ID 를 조용히 빼고 부분 목록을 준다.
    missing = [s for s in wanted if s not in by_id]
    if missing:
        raise NetworkNotFound(
            "지정한 서브넷을 찾을 수 없습니다: " + ", ".join(missing),
            remedy="서브넷 ID 와 리전을 확인하세요.",
        )

    vpc_ids = {str(by_id[s]["VpcId"]) for s in wanted}
    if len(vpc_ids) > 1:
        raise NetworkNotFound(
            "지정한 서브넷들이 서로 다른 VPC 에 있습니다: " + ", ".join(sorted(vpc_ids)),
            remedy="ECS awsvpc 네트워크 모드는 한 VPC 안의 서브넷만 받습니다. "
                   "같은 VPC 의 서브넷으로 맞춰 주세요.",
        )
    vpc_id = vpc_ids.pop()

    if len(wanted) > _MAX_AWSVPC_SUBNETS:
        raise NetworkNotFound(
            f"서브넷을 {len(wanted)}개 지정하셨는데 ECS 는 서비스 하나에 "
            f"최대 {_MAX_AWSVPC_SUBNETS}개까지만 받습니다.",
            remedy=f"{_MAX_AWSVPC_SUBNETS}개 이하로 줄여 주세요.",
        )

    # 기본 VPC 탐색과 **같은 함수**로 판정한다. 갈라 놓으면 같은 서브넷인데
    # 지정 방식에 따라 결과가 달라진다 — 실제로 그런 상태였다.
    # strict=False: 여기서는 사용자가 직접 골랐으므로 막지 않고 경고만 한다.
    chosen, verified = choose_routable_subnets(
        ec2, vpc_id, [by_id[s] for s in wanted],
        where="지정한 서브넷 중", strict=False,
    )

    # 인터넷으로 못 나가는 서브넷은 **빼고 넘긴다.** 섞어서 넘기면 태스크가
    # 어디에 배치되느냐에 따라 되기도 하고 안 되기도 한다 — 재현이 어려운
    # 최악의 실패 모양이다.
    chosen_set = set(chosen)
    dropped = [s for s in wanted if s not in chosen_set]
    if dropped:
        logger.warning(
            "인터넷으로 나가는 경로를 확인하지 못한 서브넷은 제외합니다: %s",
            ", ".join(dropped),
        )

    return NetworkTarget(
        vpc_id=vpc_id,
        subnet_ids=tuple(chosen),
        internet_routable=verified,
    )


def ensure_security_group(
    ec2: Any,
    *,
    vpc_id: str,
    name: str,
    port: int,
    description: str = "ReCoder ECS Fargate service",
    cidr: str = "0.0.0.0/0",
) -> str:
    """앱 포트를 여는 보안 그룹을 확보하고 GroupId 를 돌려준다.

    기본값이 0.0.0.0/0 인 이유는 카드 DoD 가 "URL 로 접속됨"을 요구하기
    때문이다. 프로덕션이라면 ALB 뒤로 넣고 여기는 ALB 만 허용해야 한다
    (후속 카드로 분리).
    """
    if not 1 <= port <= 65535:
        raise InfraError(f"포트 번호가 범위를 벗어납니다: {port}")

    group_id = ""
    try:
        group_id = str(
            ec2.create_security_group(
                GroupName=name, Description=description, VpcId=vpc_id
            )["GroupId"]
        )
        logger.info("보안 그룹 생성: %s (%s)", name, group_id)
    except Exception as exc:  # noqa: BLE001
        if error_code(exc) != "InvalidGroup.Duplicate":
            raise InfraError(
                f"보안 그룹 '{name}' 을 만들지 못했습니다.",
                detail=error_message(exc),
                remedy="권한표의 ec2:CreateSecurityGroup 이 부여됐는지 확인하세요.",
            ) from exc
        try:
            groups = ec2.describe_security_groups(
                Filters=[
                    {"Name": "group-name", "Values": [name]},
                    {"Name": "vpc-id", "Values": [vpc_id]},
                ]
            ).get("SecurityGroups", [])
        except Exception as inner:  # noqa: BLE001
            raise InfraError(
                f"보안 그룹 '{name}' 을 조회하지 못했습니다.",
                detail=error_message(inner),
            ) from inner
        if not groups:
            raise InfraError(
                f"보안 그룹 '{name}' 이 이미 있다고 하는데 찾을 수 없습니다.",
                remedy="다른 VPC 에 같은 이름이 있는지 확인하세요.",
            )
        group_id = str(groups[0]["GroupId"])
        logger.info("보안 그룹 재사용: %s (%s)", name, group_id)

    try:
        ec2.authorize_security_group_ingress(
            GroupId=group_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "IpRanges": [
                        {"CidrIp": cidr, "Description": "ReCoder app port"}
                    ],
                }
            ],
        )
        logger.info("보안 그룹 인바운드 추가: %s:%d ← %s", group_id, port, cidr)
    except Exception as exc:  # noqa: BLE001
        if error_code(exc) != "InvalidPermission.Duplicate":
            raise InfraError(
                f"보안 그룹 '{name}' 에 인바운드 규칙을 추가하지 못했습니다.",
                detail=error_message(exc),
                remedy="권한표의 ec2:AuthorizeSecurityGroupIngress 를 확인하세요.",
            ) from exc

    return group_id


# ---------------------------------------------------------------------------
# 서비스
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceResult:
    """`ensure_service` 결과."""

    service_arn: str
    #: "created" | "updated"
    action: str
    desired_count: int


#: "조회했더니 없더라"로 해석해도 되는 오류 코드.
#: 이 목록에 없는 오류(권한 거부, 자격증명 만료 등)를 "없음"으로 삼키면
#: 곧바로 create 를 시도하게 되고, 사용자는 진짜 원인 대신 엉뚱한
#: 생성 실패 메시지를 보게 된다.
_ABSENT_SERVICE_CODES = frozenset(
    {"ClusterNotFoundException", "ServiceNotFoundException", "ServiceNotActiveException"}
)


def _describe_service(ecs: Any, cluster: str, service: str) -> dict | None:
    """서비스를 조회한다. 없으면 None. ACTIVE 가 아니면 상태를 그대로 돌려준다."""
    try:
        resp = ecs.describe_services(cluster=cluster, services=[service])
    except Exception as exc:  # noqa: BLE001
        if error_code(exc) in _ABSENT_SERVICE_CODES:
            return None
        raise InfraError(
            f"ECS 서비스 '{service}' 의 현재 상태를 조회하지 못했습니다.",
            detail=error_message(exc),
            remedy="AWS 자격증명이 만료되지 않았는지, ecs:DescribeServices "
                   "권한이 있는지 확인하세요.",
        ) from exc
    for svc in resp.get("services", []):
        if svc.get("serviceName") == service:
            return svc
    return None


def ensure_service(
    ecs: Any,
    *,
    cluster: str,
    service: str,
    task_definition: str,
    subnet_ids: Iterable[str],
    security_group_ids: Iterable[str],
    desired_count: int = 1,
    assign_public_ip: bool = True,
    circuit_breaker: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> ServiceResult:
    """Fargate 서비스를 확보한다. 있으면 새 태스크 정의로 갱신한다.

    `desired_count=0` 으로 부르면 서비스는 만들되 태스크는 띄우지 않는다.
    Fargate 는 실행 중인 태스크에만 과금되므로 이 상태의 비용은 0원이다.

    `circuit_breaker=True` 면 **ECS 자체의** 배포 서킷 브레이커를 켠다.
    이게 없으면 컨테이너가 계속 죽는 서비스에 대해 ECS 가 대체 태스크를
    무한히 새로 띄운다. 우리 쪽 폴링 브레이커는 그걸 **관찰**만 할 뿐
    멈추게 하지는 못한다 — 기록에는 "자동 중단"이라 적히는데 AWS 에서는
    태스크가 계속 뜨고 요금이 계속 붙는 상태가 된다.

    `rollback=True` 를 함께 켜므로, 되돌아갈 이전 버전이 있으면 ECS 가
    스스로 그 버전으로 되돌린다. 첫 배포처럼 되돌아갈 곳이 없으면 배포를
    FAILED 로 끝내고 태스크 재생성을 멈춘다.
    """
    subnets = [str(s) for s in subnet_ids]
    groups = [str(g) for g in security_group_ids]
    if not subnets:
        raise InfraError("태스크를 띄울 서브넷이 지정되지 않았습니다.")
    if not groups:
        raise InfraError("태스크에 붙일 보안 그룹이 지정되지 않았습니다.")
    if desired_count < 0:
        raise InfraError(f"desired_count 는 0 이상이어야 합니다: {desired_count}")

    # UpdateService 는 이 구조체를 **통째로 교체**한다. 일부만 보내면 나머지
    # 항목이 AWS 기본값으로 되돌아간다 — 누가 조정해 둔 값이 배포할 때마다
    # 조용히 원복되는 셈이다. 그래서 두 퍼센트 값도 명시해, 기본값에 기대는
    # 대신 **우리가 고른 값**이 되게 한다.
    deployment_configuration = {
        "deploymentCircuitBreaker": {
            "enable": bool(circuit_breaker),
            "rollback": bool(circuit_breaker),
        },
        # 새 태스크가 뜬 뒤에 옛 태스크를 내린다(무중단). 갱신 중 잠깐
        # 태스크가 2개가 되지만 Fargate 는 초 단위 과금이라 몇 원 수준이다.
        "minimumHealthyPercent": 100,
        "maximumPercent": 200,
    }

    network_configuration = {
        "awsvpcConfiguration": {
            "subnets": subnets,
            "securityGroups": groups,
            "assignPublicIp": "ENABLED" if assign_public_ip else "DISABLED",
        }
    }

    existing = _describe_service(ecs, cluster, service)
    status = str(existing.get("status") or "") if existing else ""

    if existing is not None and status == "ACTIVE":
        try:
            resp = ecs.update_service(
                cluster=cluster,
                service=service,
                taskDefinition=task_definition,
                desiredCount=desired_count,
                networkConfiguration=network_configuration,
                deploymentConfiguration=deployment_configuration,
                forceNewDeployment=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise InfraError(
                f"ECS 서비스 '{service}' 를 갱신하지 못했습니다.",
                detail=error_message(exc),
                remedy="권한표의 ecs:UpdateService 가 부여됐는지 확인하세요.",
            ) from exc
        logger.info("ECS 서비스 갱신: %s/%s → %s", cluster, service, task_definition)
        return ServiceResult(
            service_arn=str(resp["service"]["serviceArn"]),
            action="updated",
            desired_count=desired_count,
        )

    if existing is not None and status == "DRAINING":
        raise InfraError(
            f"ECS 서비스 '{service}' 가 삭제 중(DRAINING)이라 지금은 쓸 수 없습니다.",
            remedy="1~2분 뒤 다시 시도하세요.",
        )

    # status 가 INACTIVE(삭제됨)이거나 서비스 자체가 없으면 새로 만든다.
    def _create() -> dict:
        return ecs.create_service(
            cluster=cluster,
            serviceName=service,
            taskDefinition=task_definition,
            desiredCount=desired_count,
            launchType="FARGATE",
            networkConfiguration=network_configuration,
            deploymentConfiguration=deployment_configuration,
        )

    try:
        resp = retry_on_transient(_create, sleep=sleep)
    except InfraError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InfraError(
            f"ECS 서비스 '{service}' 를 만들지 못했습니다.",
            detail=error_message(exc),
            remedy="권한표의 ecs:CreateService 와 iam:PassRole 을 확인하세요.",
        ) from exc

    logger.info("ECS 서비스 생성: %s/%s (desired=%d)", cluster, service, desired_count)
    return ServiceResult(
        service_arn=str(resp["service"]["serviceArn"]),
        action="created",
        desired_count=desired_count,
    )


def scale_service(ecs: Any, *, cluster: str, service: str, desired_count: int) -> int:
    """서비스의 태스크 수를 바꾼다. 0 으로 내리면 과금이 멈춘다.

    카드 DoD 에는 없는 기능이지만, 랩은 EC2 인스턴스만 자동으로 멈추고
    Fargate 태스크는 세션이 끝나도 계속 돌면서 과금된다. 방치하면
    월 $12.7 이라 $50 예산이 넉 달 만에 사라진다.
    """
    if desired_count < 0:
        raise InfraError(f"desired_count 는 0 이상이어야 합니다: {desired_count}")
    try:
        resp = ecs.update_service(
            cluster=cluster, service=service, desiredCount=desired_count
        )
    except Exception as exc:  # noqa: BLE001
        raise InfraError(
            f"ECS 서비스 '{service}' 의 태스크 수를 바꾸지 못했습니다.",
            detail=error_message(exc),
            remedy="서비스가 존재하는지, ecs:UpdateService 권한이 있는지 확인하세요.",
        ) from exc
    return int(resp["service"].get("desiredCount", desired_count))


def halt_service(ecs: Any, *, cluster: str, service: str) -> str:
    """실패한 배포의 태스크 재생성을 멈춘다 (desiredCount → 0).

    배포가 실패했고 **떠 있는 태스크가 하나도 없을 때** 부른다. 그냥 두면
    ECS 가 죽는 태스크를 계속 새로 띄우고, 우리 기록에는 "자동 중단"이라고
    적혀 있는 동안 요금만 쌓인다.

    판단은 호출자(`ECSAgent._halt_failed_deployment`)가 한다. 예전에는
    "되돌릴 이전 리비전이 있는가"로 갈랐는데, 그건 그 리비전이 지금 멀쩡히
    떠 있다는 뜻이 아니어서 틀린 기준이었다.

    실패해도 예외를 올리지 않는다. 이 함수는 **이미 실패를 처리하는 중**에
    불리므로, 여기서 터지면 원래 실패 원인이 이 실패에 가려진다.
    돌려주는 문자열은 사용자에게 보여줄 결과 설명이다.
    """
    try:
        scale_service(ecs, cluster=cluster, service=service, desired_count=0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("실패한 배포를 멈추지 못했습니다: %s", error_message(exc))
        return (
            "태스크를 자동으로 멈추지 못했습니다 — AWS 콘솔에서 서비스 "
            f"'{service}' 의 원하는 태스크 수를 0 으로 내려 과금을 멈추세요."
        )
    logger.warning("실패한 배포 중단: %s/%s 태스크 수를 0 으로 내렸습니다.", cluster, service)
    return (
        f"추가 과금을 막기 위해 서비스 '{service}' 의 태스크 수를 0 으로 "
        "내렸습니다. 원인을 고친 뒤 다시 배포하면 복구됩니다."
    )


# ---------------------------------------------------------------------------
# 공개 주소 확인 — 카드 DoD 1번 "URL 로 접속됨"
# ---------------------------------------------------------------------------


def _task_eni_ids(tasks: Iterable[dict]) -> list[str]:
    """RUNNING 태스크들이 물고 있는 ENI ID 목록."""
    eni_ids: list[str] = []
    for task in tasks:
        if task.get("lastStatus") != "RUNNING":
            continue
        for attachment in task.get("attachments", []):
            # moto 등 일부 구현은 type 을 비워 보낸다 — 비어 있으면 통과시킨다.
            att_type = attachment.get("type")
            if att_type and att_type != "ElasticNetworkInterface":
                continue
            for detail in attachment.get("details", []):
                if detail.get("name") == "networkInterfaceId" and detail.get("value"):
                    eni_ids.append(str(detail["value"]))
    return eni_ids


@dataclass
class _Probe:
    """`resolve_public_ip` 한 번의 결과.

    "아직 안 떴다"와 "호출이 실패했다"를 **구분해서** 들고 다닌다.
    둘 다 None 으로 뭉개면, 권한이 없어 매번 거부당하는 상황에서도
    사용자는 5분을 기다린 끝에 "공인 IP 를 못 받았습니다"라는 엉뚱한
    메시지를 보게 된다. 진짜 원인(권한 거부)은 어디에도 안 나온다.
    """

    ip: Optional[str] = None
    last_error: Optional[str] = None
    #: 어느 단계까지 갔는지 — 실패 메시지를 구체적으로 쓰기 위해.
    stage: str = "list_tasks"


def _probe_public_ip(ecs: Any, ec2: Any, *, cluster: str, service: str) -> _Probe:
    probe = _Probe()
    try:
        arns = ecs.list_tasks(
            cluster=cluster, serviceName=service, desiredStatus="RUNNING"
        ).get("taskArns", [])
    except Exception as exc:  # noqa: BLE001
        probe.last_error = f"list_tasks: {error_message(exc)}"
        return probe
    if not arns:
        return probe

    probe.stage = "describe_tasks"
    try:
        # describe_tasks 는 한 번에 100개까지 받는다.
        tasks = ecs.describe_tasks(cluster=cluster, tasks=list(arns)[:100]).get(
            "tasks", []
        )
    except Exception as exc:  # noqa: BLE001
        probe.last_error = f"describe_tasks: {error_message(exc)}"
        return probe

    eni_ids = _task_eni_ids(tasks)
    if not eni_ids:
        probe.stage = "await_eni"
        return probe

    probe.stage = "describe_network_interfaces"
    try:
        interfaces = ec2.describe_network_interfaces(
            NetworkInterfaceIds=eni_ids[:100]
        ).get("NetworkInterfaces", [])
    except Exception as exc:  # noqa: BLE001
        probe.last_error = f"describe_network_interfaces: {error_message(exc)}"
        return probe

    for interface in interfaces:
        association = interface.get("Association") or {}
        public_ip = association.get("PublicIp")
        if public_ip:
            probe.ip = str(public_ip)
            probe.stage = "done"
            return probe
    probe.stage = "await_public_ip"
    return probe


def resolve_public_ip(
    ecs: Any, ec2: Any, *, cluster: str, service: str
) -> Optional[str]:
    """서비스가 띄운 태스크의 공인 IP 를 찾는다. 아직이면 None.

    None 은 실패가 아니라 "아직"이다. 태스크가 RUNNING 이 되고 ENI 에
    공인 IP 가 붙기까지는 시간이 걸린다 — 호출자가 재시도해야 한다.
    원인까지 알아야 하면 `_probe_public_ip` 를 직접 쓴다.
    """
    return _probe_public_ip(ecs, ec2, cluster=cluster, service=service).ip


def wait_for_public_url(
    ecs: Any,
    ec2: Any,
    *,
    cluster: str,
    service: str,
    port: int,
    scheme: str = "http",
    timeout: float = 300.0,
    interval: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """태스크에 공인 IP 가 붙을 때까지 기다렸다가 접속 URL 을 만든다."""
    if timeout <= 0:
        raise InfraError("timeout 은 0보다 커야 합니다.")
    deadline = monotonic() + timeout
    attempts = 0
    probe = _Probe()
    while True:
        attempts += 1
        probe = _probe_public_ip(ecs, ec2, cluster=cluster, service=service)
        if probe.ip:
            url = (
                f"{scheme}://{probe.ip}"
                if port in (80, 443)
                else f"{scheme}://{probe.ip}:{port}"
            )
            logger.info("공개 주소 확인: %s (%d회 시도)", url, attempts)
            return url
        if monotonic() >= deadline:
            break
        sleep(interval)

    # 마지막 시도가 **호출 실패**였다면 그걸 원인으로 보고한다.
    # "IP 를 못 받았다"로 뭉개면 진짜 원인이 사라진다.
    if probe.last_error:
        raise InfraError(
            "태스크의 공인 IP 를 확인하는 중 AWS 호출이 계속 실패했습니다.",
            detail=probe.last_error,
            remedy="권한표에 ecs:ListTasks, ecs:DescribeTasks, "
                   "ec2:DescribeNetworkInterfaces 가 있는지, AWS 자격증명이 "
                   "만료되지 않았는지 확인하세요.",
        )

    if probe.stage == "list_tasks":
        raise InfraError(
            f"{int(timeout)}초 안에 실행 중인 태스크가 하나도 뜨지 않았습니다.",
            remedy="CloudWatch 로그에서 컨테이너가 시작 직후 종료되는지 "
                   "확인하세요. 이미지 pull 실패나 앱 기동 오류가 흔한 원인입니다.",
        )

    raise InfraError(
        f"태스크는 시작됐지만 {int(timeout)}초 안에 공인 IP 를 받지 못했습니다.",
        detail=f"마지막 단계: {probe.stage}",
        remedy="서비스의 assignPublicIp 가 ENABLED 인지, 서브넷이 퍼블릭인지 "
               "확인하세요.",
    )
