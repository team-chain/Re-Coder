"""
core/aws_infra.py 테스트 — FR-05-04

이 파일이 지키려는 성질은 두 가지다.

1. **멱등성.** 모든 ensure_* 는 두 번 불러도 같은 결과를 내야 한다.
   AWS Academy 랩은 4시간마다 세션이 끊기지만 리소스는 남는다.
   두 번째 실행이 "이미 있음"으로 죽으면 그건 완성이 아니다.

2. **실패가 실패로 보일 것.** 권한 거부를 "아직 준비 안 됨"으로 삼키거나,
   확인 못 한 것을 확인한 것처럼 돌려주면 안 된다. 이 파일의 부정 통제
   (negative control) 테스트들이 그걸 막는다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aws_infra  # noqa: E402

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

REGION = "us-east-1"


# ---------------------------------------------------------------------------
# 가짜 클라이언트 — 특정 오류를 재현할 때 쓴다
# ---------------------------------------------------------------------------


def client_error(code: str, message: str, operation: str = "Op") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}}, operation
    )


class Boom:
    """부르면 지정한 예외를 던지는 스텁."""

    def __init__(self, exc: BaseException, *, calls: list | None = None) -> None:
        self.exc = exc
        self.calls = calls if calls is not None else []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise self.exc


# ---------------------------------------------------------------------------
# 오류 헬퍼
# ---------------------------------------------------------------------------


def test_error_code_reads_aws_code():
    assert aws_infra.error_code(client_error("AccessDenied", "nope")) == "AccessDenied"


def test_error_code_is_empty_for_plain_exceptions():
    assert aws_infra.error_code(ValueError("boom")) == ""


def test_error_message_falls_back_to_str():
    assert aws_infra.error_message(ValueError("boom")) == "boom"


# ---------------------------------------------------------------------------
# 서비스 연결 롤 재시도 — 랩 Readme 에 명시된 알려진 현상
# ---------------------------------------------------------------------------


def test_service_linked_role_error_is_recognised():
    exc = client_error(
        "InvalidParameterException",
        "Unable to assume the service linked role. Please verify that the ECS "
        "service linked role exists.",
    )
    assert aws_infra.is_service_linked_role_race(exc) is True


def test_unrelated_error_is_not_treated_as_the_service_linked_role_race():
    """부정 통제: 권한 거부를 일시적 오류로 오인하면 안 된다.

    오인하면 영구 실패에 대해 네 번 기다린 뒤 엉뚱한 원인을 보고하게 된다.
    """
    exc = client_error("AccessDeniedException", "User is not authorized")
    assert aws_infra.is_service_linked_role_race(exc) is False


def test_retry_gives_up_and_reports_a_human_message():
    exc = client_error("InvalidParameterException", "ECS service linked role missing")
    boom = Boom(exc)
    with pytest.raises(aws_infra.InfraError) as caught:
        aws_infra.retry_on_transient(boom, attempts=3, sleep=lambda _: None)
    assert len(boom.calls) == 3
    assert "서비스 연결 역할" in str(caught.value)
    # AWS 원문을 버리지 않는다
    assert "service linked role" in caught.value.detail


def test_retry_succeeds_after_a_transient_failure():
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise client_error(
                "InvalidParameterException", "ECS service linked role not ready"
            )
        return "ok"

    assert aws_infra.retry_on_transient(flaky, attempts=4, sleep=lambda _: None) == "ok"
    assert state["n"] == 3


def test_retry_does_not_swallow_permanent_errors():
    """부정 통제: 재시도 대상이 아닌 예외는 **즉시** 원형 그대로 올라와야 한다."""
    boom = Boom(client_error("AccessDeniedException", "denied"))
    with pytest.raises(ClientError):
        aws_infra.retry_on_transient(boom, attempts=5, sleep=lambda _: None)
    assert len(boom.calls) == 1, "재시도 대상이 아닌데 재시도했다"


# ---------------------------------------------------------------------------
# 클러스터
# ---------------------------------------------------------------------------


@mock_aws
def test_ensure_cluster_creates_then_reuses():
    ecs = boto3.client("ecs", region_name=REGION)
    first = aws_infra.ensure_cluster(ecs, "recoder-test")
    second = aws_infra.ensure_cluster(ecs, "recoder-test")
    assert first == second
    assert len(ecs.list_clusters()["clusterArns"]) == 1


@mock_aws
def test_ensure_cluster_reports_a_human_message_on_failure():
    ecs = boto3.client("ecs", region_name=REGION)
    ecs.describe_clusters = Boom(client_error("AccessDeniedException", "denied"))
    with pytest.raises(aws_infra.InfraError) as caught:
        aws_infra.ensure_cluster(ecs, "recoder-test")
    assert "조회하지 못했습니다" in str(caught.value)
    assert caught.value.remedy


# ---------------------------------------------------------------------------
# 로그 그룹
# ---------------------------------------------------------------------------


@mock_aws
def test_ensure_log_group_is_idempotent_and_sets_retention():
    logs = boto3.client("logs", region_name=REGION)
    aws_infra.ensure_log_group(logs, "/ecs/recoder-test", retention_days=7)
    aws_infra.ensure_log_group(logs, "/ecs/recoder-test", retention_days=7)
    groups = logs.describe_log_groups(logGroupNamePrefix="/ecs/recoder-test")[
        "logGroups"
    ]
    assert len(groups) == 1
    assert groups[0]["retentionInDays"] == 7


@mock_aws
def test_ensure_log_group_fixes_retention_on_a_pre_existing_group():
    """보관 기간 없이 만들어진 기존 그룹도 고쳐줘야 한다 — 안 그러면 영원히 쌓인다."""
    logs = boto3.client("logs", region_name=REGION)
    logs.create_log_group(logGroupName="/ecs/legacy")
    before = logs.describe_log_groups(logGroupNamePrefix="/ecs/legacy")["logGroups"][0]
    assert "retentionInDays" not in before

    aws_infra.ensure_log_group(logs, "/ecs/legacy", retention_days=7)
    after = logs.describe_log_groups(logGroupNamePrefix="/ecs/legacy")["logGroups"][0]
    assert after["retentionInDays"] == 7


def test_invalid_retention_is_rejected_before_calling_aws():
    """CloudWatch 가 받지 않는 값은 우리가 먼저 거른다."""
    with pytest.raises(aws_infra.InfraError) as caught:
        aws_infra.ensure_log_group(object(), "/ecs/x", retention_days=9)
    assert "받지 않는 값" in str(caught.value)


# ---------------------------------------------------------------------------
# ECR
# ---------------------------------------------------------------------------


@mock_aws
def test_ensure_ecr_repository_is_idempotent_and_returns_the_same_uri():
    ecr = boto3.client("ecr", region_name=REGION)
    first = aws_infra.ensure_ecr_repository(ecr, "recoder-test")
    second = aws_infra.ensure_ecr_repository(ecr, "recoder-test")
    assert first == second
    assert first.endswith("/recoder-test")
    assert len(ecr.describe_repositories()["repositories"]) == 1


@mock_aws
def test_ensure_ecr_repository_applies_a_lifecycle_policy():
    ecr = boto3.client("ecr", region_name=REGION)
    aws_infra.ensure_ecr_repository(ecr, "recoder-test", keep_last=5)
    policy = json.loads(
        ecr.get_lifecycle_policy(repositoryName="recoder-test")["lifecyclePolicyText"]
    )
    rule = policy["rules"][0]
    assert rule["selection"]["countNumber"] == 5
    assert rule["action"]["type"] == "expire"


@mock_aws
def test_bad_keep_last_fails_loudly_instead_of_becoming_a_warning():
    """부정 통제: 수명 주기 실패는 경고로 넘기지만, **잘못된 인자**는 아니다.

    이 구분이 없으면 keep_last 오타가 조용한 경고 한 줄로 묻히고
    이미지가 계속 쌓인다.
    """
    ecr = boto3.client("ecr", region_name=REGION)
    with pytest.raises(aws_infra.InfraError):
        aws_infra.ensure_ecr_repository(ecr, "recoder-test", keep_last=0)


# ---------------------------------------------------------------------------
# 네트워킹
# ---------------------------------------------------------------------------


@mock_aws
def test_discover_default_network_finds_public_subnets():
    ec2 = boto3.client("ec2", region_name=REGION)
    target = aws_infra.discover_default_network(ec2)
    assert target.vpc_id.startswith("vpc-")
    assert target.subnet_ids
    # 결정적이어야 한다 — 두 번 불러 같은 결과.
    # 무작위로 바뀌면 서비스가 매번 갱신되고 테스트도 흔들린다.
    assert aws_infra.discover_default_network(ec2).subnet_ids == target.subnet_ids


@mock_aws
def test_igw_routing_is_the_primary_signal():
    """1차 신호(라우팅에 igw- 경로)가 동작하는지 직접 확인한다.

    moto 의 기본 VPC 에는 인터넷 게이트웨이가 없어서(실제 AWS 와 다름)
    기본 VPC 로는 이 경로를 검증할 수 없다. 그래서 실제 계정과 같은 모양
    — IGW + 0.0.0.0/0 경로 — 을 손으로 만들어 확인한다.
    (실계정 확인 결과: 기본 VPC 메인 라우트 테이블에 local + igw- 경로 존재)
    """
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc = ec2.create_vpc(CidrBlock="10.42.0.0/16")["Vpc"]
    subnet = ec2.create_subnet(VpcId=vpc["VpcId"], CidrBlock="10.42.1.0/24")["Subnet"]
    igw = ec2.create_internet_gateway()["InternetGateway"]
    ec2.attach_internet_gateway(
        InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc["VpcId"]
    )
    table = ec2.create_route_table(VpcId=vpc["VpcId"])["RouteTable"]
    ec2.create_route(
        RouteTableId=table["RouteTableId"],
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=igw["InternetGatewayId"],
    )
    ec2.associate_route_table(
        RouteTableId=table["RouteTableId"], SubnetId=subnet["SubnetId"]
    )

    routable, verified = aws_infra.internet_routable_subnets(
        ec2, vpc["VpcId"], [subnet["SubnetId"]]
    )
    assert verified is True
    assert routable == [subnet["SubnetId"]]


@mock_aws
def test_public_ip_flag_is_the_fallback_signal():
    """2차 신호. IGW 경로를 못 찾아도 공인 IP 자동 할당이 켜져 있으면
    막지 않는다 — 정상 환경을 검증 로직으로 깨뜨리지 않기 위해서다.
    다만 `internet_routable=False` 로 "확인되지 않았음"을 남긴다."""
    ec2 = boto3.client("ec2", region_name=REGION)
    target = aws_infra.discover_default_network(ec2)
    assert target.subnet_ids
    assert target.internet_routable is False


@mock_aws
def test_discover_default_network_caps_the_subnet_count():
    ec2 = boto3.client("ec2", region_name=REGION)
    target = aws_infra.discover_default_network(ec2, max_subnets=2)
    assert len(target.subnet_ids) <= 2


@mock_aws
def test_subnets_without_an_internet_gateway_are_rejected():
    """부정 통제: 인터넷으로 못 나가는 서브넷을 골라주면 이미지 pull 이
    실패하고, 그 오류는 원인을 짐작하기 어렵다. 배포 전에 막아야 한다."""
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc = ec2.create_vpc(CidrBlock="10.9.0.0/16")["Vpc"]
    ec2.create_subnet(VpcId=vpc["VpcId"], CidrBlock="10.9.1.0/24")
    # 기본 VPC 인 척하게 만든다
    ec2.modify_vpc_attribute  # noqa: B018 - 존재 확인용

    subnets = ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}]
    )["Subnets"]
    ids = [s["SubnetId"] for s in subnets]
    routable, verified = aws_infra.internet_routable_subnets(ec2, vpc["VpcId"], ids)
    assert verified is True
    assert routable == [], "IGW 경로가 없는데 인터넷 가능으로 판정했다"


@mock_aws
def test_unverifiable_routing_is_reported_as_unverified_not_as_verified():
    """부정 통제: 라우팅을 **확인 못 한 것**을 확인한 것처럼 돌려주면 안 된다."""
    ec2 = boto3.client("ec2", region_name=REGION)
    ec2.describe_route_tables = Boom(client_error("AccessDeniedException", "denied"))
    routable, verified = aws_infra.internet_routable_subnets(
        ec2, "vpc-123", ["subnet-a", "subnet-b"]
    )
    assert routable == ["subnet-a", "subnet-b"]
    assert verified is False, "확인 실패를 확인 성공으로 보고했다"


@mock_aws
def test_discover_refuses_when_both_signals_say_no_internet():
    """부정 통제: 두 신호 모두 "인터넷 못 나감"일 때는 **막아야** 한다.

    이대로 배포하면 컨테이너가 ECR 에서 이미지를 못 받고, 그때 나오는
    오류는 원인을 짐작하기 어렵다. moto 의 기본 VPC 는 서브넷이 항상
    공인 IP 자동 할당이라 2차 신호가 늘 켜져 있으므로, 여기서는 응답을
    직접 만들어 그 분기를 친다.
    """
    ec2 = boto3.client("ec2", region_name=REGION)
    ec2.describe_vpcs = lambda **_: {"Vpcs": [{"VpcId": "vpc-private"}]}
    ec2.describe_subnets = lambda **_: {
        "Subnets": [
            {
                "SubnetId": "subnet-private",
                "AvailabilityZone": "us-east-1a",
                "MapPublicIpOnLaunch": False,
            }
        ]
    }
    # 라우팅은 조회되지만 igw- 경로가 없다 → 1차 신호도 "아니오"
    ec2.describe_route_tables = lambda **_: {
        "RouteTables": [
            {
                "Associations": [{"Main": True}],
                "Routes": [{"DestinationCidrBlock": "10.0.0.0/16",
                            "GatewayId": "local"}],
            }
        ]
    }
    with pytest.raises(aws_infra.NetworkNotFound) as caught:
        aws_infra.discover_default_network(ec2)
    assert "인터넷으로 나가는 서브넷이 없습니다" in str(caught.value)
    assert caught.value.remedy


@mock_aws
def test_missing_default_vpc_produces_an_actionable_message():
    ec2 = boto3.client("ec2", region_name=REGION)
    ec2.describe_vpcs = lambda **_: {"Vpcs": []}
    with pytest.raises(aws_infra.NetworkNotFound) as caught:
        aws_infra.discover_default_network(ec2)
    assert "기본 VPC" in str(caught.value)
    assert caught.value.remedy


@mock_aws
def test_ensure_security_group_is_idempotent():
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id = aws_infra.discover_default_network(ec2).vpc_id
    first = aws_infra.ensure_security_group(
        ec2, vpc_id=vpc_id, name="recoder-test-sg", port=8000
    )
    second = aws_infra.ensure_security_group(
        ec2, vpc_id=vpc_id, name="recoder-test-sg", port=8000
    )
    assert first == second

    groups = ec2.describe_security_groups(GroupIds=[first])["SecurityGroups"]
    perms = groups[0]["IpPermissions"]
    matching = [p for p in perms if p.get("FromPort") == 8000]
    assert len(matching) == 1, "같은 규칙이 중복 추가됐다"


@mock_aws
def test_ensure_security_group_rejects_a_bad_port():
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id = aws_infra.discover_default_network(ec2).vpc_id
    with pytest.raises(aws_infra.InfraError):
        aws_infra.ensure_security_group(
            ec2, vpc_id=vpc_id, name="recoder-test-sg", port=0
        )


# ---------------------------------------------------------------------------
# 서비스
# ---------------------------------------------------------------------------


def _register_task_definition(ecs) -> str:
    resp = ecs.register_task_definition(
        family="recoder-test",
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="256",
        memory="512",
        containerDefinitions=[
            {"name": "app", "image": "nginx:alpine", "essential": True}
        ],
    )
    return resp["taskDefinition"]["taskDefinitionArn"]


@mock_aws
def _make_service_fixture():
    raise NotImplementedError  # pragma: no cover


@mock_aws
def test_ensure_service_creates_then_updates():
    ecs = boto3.client("ecs", region_name=REGION)
    ec2 = boto3.client("ec2", region_name=REGION)
    net = aws_infra.discover_default_network(ec2)
    sg = aws_infra.ensure_security_group(
        ec2, vpc_id=net.vpc_id, name="recoder-test-sg", port=8000
    )
    aws_infra.ensure_cluster(ecs, "recoder-test")
    task_def = _register_task_definition(ecs)

    first = aws_infra.ensure_service(
        ecs,
        cluster="recoder-test",
        service="recoder-test-svc",
        task_definition=task_def,
        subnet_ids=net.subnet_ids,
        security_group_ids=[sg],
        desired_count=0,
    )
    assert first.action == "created"

    second = aws_infra.ensure_service(
        ecs,
        cluster="recoder-test",
        service="recoder-test-svc",
        task_definition=task_def,
        subnet_ids=net.subnet_ids,
        security_group_ids=[sg],
        desired_count=0,
    )
    assert second.action == "updated", "두 번째 실행이 서비스를 또 만들려 했다"
    assert len(ecs.list_services(cluster="recoder-test")["serviceArns"]) == 1


@mock_aws
def test_ensure_service_rejects_empty_networking():
    ecs = boto3.client("ecs", region_name=REGION)
    aws_infra.ensure_cluster(ecs, "recoder-test")
    task_def = _register_task_definition(ecs)
    with pytest.raises(aws_infra.InfraError):
        aws_infra.ensure_service(
            ecs,
            cluster="recoder-test",
            service="svc",
            task_definition=task_def,
            subnet_ids=[],
            security_group_ids=["sg-1"],
        )


@mock_aws
def test_describe_service_does_not_swallow_permission_errors():
    """부정 통제: 조회 실패를 "서비스 없음"으로 삼키면, 곧바로 create 를
    시도하게 되고 사용자는 진짜 원인 대신 엉뚱한 생성 실패를 보게 된다."""
    ecs = boto3.client("ecs", region_name=REGION)
    ecs.describe_services = Boom(client_error("AccessDeniedException", "denied"))
    with pytest.raises(aws_infra.InfraError) as caught:
        aws_infra.ensure_service(
            ecs,
            cluster="c",
            service="s",
            task_definition="td",
            subnet_ids=["subnet-a"],
            security_group_ids=["sg-a"],
        )
    assert "상태를 조회하지 못했습니다" in str(caught.value)


@mock_aws
def test_absent_service_is_not_an_error():
    """클러스터가 없다는 응답은 "서비스 없음"으로 봐야 한다."""
    ecs = boto3.client("ecs", region_name=REGION)
    ecs.describe_services = Boom(
        client_error("ClusterNotFoundException", "cluster not found")
    )
    created = {}
    ecs.create_service = lambda **kw: created.update(kw) or {
        "service": {"serviceArn": "arn:aws:ecs:::service/x"}
    }
    result = aws_infra.ensure_service(
        ecs,
        cluster="c",
        service="s",
        task_definition="td",
        subnet_ids=["subnet-a"],
        security_group_ids=["sg-a"],
        desired_count=0,
    )
    assert result.action == "created"
    assert created["launchType"] == "FARGATE"
    assert (
        created["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"]
        == "ENABLED"
    )


@mock_aws
def test_scale_service_to_zero():
    ecs = boto3.client("ecs", region_name=REGION)
    ec2 = boto3.client("ec2", region_name=REGION)
    net = aws_infra.discover_default_network(ec2)
    sg = aws_infra.ensure_security_group(
        ec2, vpc_id=net.vpc_id, name="recoder-test-sg", port=8000
    )
    aws_infra.ensure_cluster(ecs, "recoder-test")
    task_def = _register_task_definition(ecs)
    aws_infra.ensure_service(
        ecs,
        cluster="recoder-test",
        service="recoder-test-svc",
        task_definition=task_def,
        subnet_ids=net.subnet_ids,
        security_group_ids=[sg],
        desired_count=1,
    )
    assert (
        aws_infra.scale_service(
            ecs, cluster="recoder-test", service="recoder-test-svc", desired_count=0
        )
        == 0
    )


# ---------------------------------------------------------------------------
# 공개 주소 — DoD 1번
# ---------------------------------------------------------------------------


class FakeEcs:
    def __init__(self, task_arns, tasks):
        self._arns = task_arns
        self._tasks = tasks

    def list_tasks(self, **_):
        return {"taskArns": self._arns}

    def describe_tasks(self, **_):
        return {"tasks": self._tasks}


class FakeEc2:
    def __init__(self, interfaces):
        self._interfaces = interfaces

    def describe_network_interfaces(self, **_):
        return {"NetworkInterfaces": self._interfaces}


def _running_task(eni_id="eni-1"):
    return {
        "lastStatus": "RUNNING",
        "attachments": [
            {
                "type": "ElasticNetworkInterface",
                "details": [{"name": "networkInterfaceId", "value": eni_id}],
            }
        ],
    }


def test_resolve_public_ip_walks_task_to_eni_to_ip():
    ecs = FakeEcs(["arn:task/1"], [_running_task()])
    ec2 = FakeEc2([{"Association": {"PublicIp": "54.1.2.3"}}])
    assert (
        aws_infra.resolve_public_ip(ecs, ec2, cluster="c", service="s") == "54.1.2.3"
    )


def test_resolve_public_ip_returns_none_while_the_task_is_still_starting():
    ecs = FakeEcs(["arn:task/1"], [{"lastStatus": "PENDING", "attachments": []}])
    ec2 = FakeEc2([])
    assert aws_infra.resolve_public_ip(ecs, ec2, cluster="c", service="s") is None


def test_a_task_that_is_not_running_is_skipped_even_if_it_has_an_eni():
    """부정 통제: RUNNING 필터가 실제로 동작해야 한다.

    이 테스트가 없을 때 필터를 통째로 지워도 전부 통과했다 — 위 테스트가
    `attachments: []` 를 주는 바람에 필터를 거치지 않고 통과했기 때문이다.
    여기서는 **ENI 가 붙어 있는** 비-RUNNING 태스크를 준다. 필터가 없으면
    종료 중인 태스크의 낡은 공인 IP 가 서비스 주소로 나간다.
    """
    stopping = {
        "lastStatus": "DEACTIVATING",
        "attachments": [
            {
                "type": "ElasticNetworkInterface",
                "details": [{"name": "networkInterfaceId", "value": "eni-old"}],
            }
        ],
    }
    ecs = FakeEcs(["arn:task/1"], [stopping])
    ec2 = FakeEc2([{"Association": {"PublicIp": "1.2.3.4"}}])
    assert aws_infra.resolve_public_ip(ecs, ec2, cluster="c", service="s") is None, \
        "종료 중인 태스크의 낡은 IP 를 서비스 주소로 돌려줬다"


def test_a_running_task_is_preferred_over_a_stopping_one():
    """섞여 있을 때 RUNNING 쪽 IP 를 골라야 한다."""
    stopping = {
        "lastStatus": "STOPPED",
        "attachments": [{"type": "ElasticNetworkInterface",
                         "details": [{"name": "networkInterfaceId",
                                      "value": "eni-old"}]}],
    }
    ecs = FakeEcs(["a", "b"], [stopping, _running_task("eni-new")])

    seen: list[list[str]] = []

    class RecordingEc2:
        def describe_network_interfaces(self, **kwargs):
            seen.append(list(kwargs["NetworkInterfaceIds"]))
            return {"NetworkInterfaces": [{"Association": {"PublicIp": "5.6.7.8"}}]}

    assert (
        aws_infra.resolve_public_ip(ecs, RecordingEc2(), cluster="c", service="s")
        == "5.6.7.8"
    )
    assert seen == [["eni-new"]], f"종료된 태스크의 ENI 를 조회했다: {seen}"


def test_wait_for_public_url_builds_the_url_and_omits_port_80():
    ecs = FakeEcs(["arn:task/1"], [_running_task()])
    ec2 = FakeEc2([{"Association": {"PublicIp": "54.1.2.3"}}])
    assert (
        aws_infra.wait_for_public_url(
            ecs, ec2, cluster="c", service="s", port=80, sleep=lambda _: None
        )
        == "http://54.1.2.3"
    )
    assert (
        aws_infra.wait_for_public_url(
            ecs, ec2, cluster="c", service="s", port=8000, sleep=lambda _: None
        )
        == "http://54.1.2.3:8000"
    )


def test_wait_for_public_url_reports_permission_errors_not_a_timeout_story():
    """부정 통제: 권한 거부로 계속 실패하는데 "IP 를 못 받았습니다"라고
    보고하면 사용자는 영원히 엉뚱한 곳을 본다."""

    class Denied:
        def list_tasks(self, **_):
            raise client_error("AccessDeniedException", "not authorized: ListTasks")

    ticks = iter([0.0, 0.0, 999.0, 999.0, 999.0])
    with pytest.raises(aws_infra.InfraError) as caught:
        aws_infra.wait_for_public_url(
            Denied(),
            FakeEc2([]),
            cluster="c",
            service="s",
            port=8000,
            timeout=1.0,
            sleep=lambda _: None,
            monotonic=lambda: next(ticks),
        )
    assert "AWS 호출이 계속 실패" in str(caught.value)
    assert "ListTasks" in caught.value.detail


def test_wait_for_public_url_distinguishes_no_tasks_from_no_ip():
    """태스크가 아예 안 뜬 것과, 떴는데 IP 가 없는 것은 원인이 다르다."""
    ticks = iter([0.0, 0.0, 999.0, 999.0, 999.0])
    with pytest.raises(aws_infra.InfraError) as caught:
        aws_infra.wait_for_public_url(
            FakeEcs([], []),
            FakeEc2([]),
            cluster="c",
            service="s",
            port=8000,
            timeout=1.0,
            sleep=lambda _: None,
            monotonic=lambda: next(ticks),
        )
    assert "실행 중인 태스크가 하나도" in str(caught.value)
