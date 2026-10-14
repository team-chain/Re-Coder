from unittest.mock import MagicMock

import pytest
from api.routes.deploy_ecs import ExtensionEcsDeployRequest, to_core_request
from aws_infra import ensure_service
from pydantic import ValidationError

ARN = "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:targetgroup/shop/012abc"


def test_extension_preserves_https_and_private_network():
    body = ExtensionEcsDeployRequest(
        target_group_arn=ARN,
        cloudfront_domain="d123.cloudfront.net",
        assign_public_ip=False,
        subnet_ids=["subnet-abc"],
        security_group_ids=["sg-abc"],
    )
    req = to_core_request(body)
    assert req.target_group_arn == ARN
    assert req.cloudfront_domain == "d123.cloudfront.net"
    assert req.assign_public_ip is False
    assert req.subnet_ids == ["subnet-abc"] and req.security_group_ids == ["sg-abc"]


@pytest.mark.parametrize(
    "domain",
    [
        "http://localhost",
        "d123.cloudfront.net/path",
        "evil.example",
        "d123.cloudfront.net@evil.example",
    ],
)
def test_health_probe_rejects_arbitrary_endpoint(domain):
    with pytest.raises(ValidationError):
        ExtensionEcsDeployRequest(cloudfront_domain=domain)


@pytest.mark.parametrize("existing", [False, True])
def test_service_attaches_alb_and_disables_public_ip(existing):
    ecs = MagicMock()
    ecs.describe_services.return_value = {
        "services": [{"serviceName": "shop", "status": "ACTIVE"}] if existing else []
    }
    ecs.create_service.return_value = ecs.update_service.return_value = {
        "service": {"serviceArn": "arn"}
    }
    ensure_service(
        ecs,
        cluster="cluster",
        service="shop",
        task_definition="td",
        subnet_ids=["subnet-abc"],
        security_group_ids=["sg-abc"],
        assign_public_ip=False,
        target_group_arn=ARN,
        container_name="web",
        container_port=3001,
    )
    call = (ecs.update_service if existing else ecs.create_service).call_args.kwargs
    assert call["loadBalancers"] == [
        {"targetGroupArn": ARN, "containerName": "web", "containerPort": 3001}
    ]
    assert call["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"] == "DISABLED"
    assert call["healthCheckGracePeriodSeconds"] == 60


def test_health_path_404_is_not_a_success(monkeypatch):
    import urllib.error
    import urllib.request

    from agents.ecs_agent import _probe_http

    def fail(*a, **kw):
        raise urllib.error.HTTPError("https://d123.cloudfront.net/health", 404, "missing", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    passed, detail = _probe_http(
        "https://d123.cloudfront.net/health", require_success=True, attempts=1
    )
    assert not passed and "404" in detail


def target_network():
    ec2, alb = MagicMock(), MagicMock()
    ec2.describe_subnets.return_value = {
        "Subnets": [{"SubnetId": "subnet-a", "VpcId": "vpc-a", "AvailabilityZone": "region-a"}]
    }
    alb.describe_target_groups.return_value = {
        "TargetGroups": [{"TargetType": "ip", "VpcId": "vpc-a", "LoadBalancerArns": ["lb"]}]
    }
    alb.describe_load_balancers.return_value = {
        "LoadBalancers": [
            {"AvailabilityZones": [{"ZoneName": "region-a"}, {"ZoneName": "region-b"}]}
        ]
    }
    return ec2, alb


def test_alb_requires_explicit_security_group_before_any_provisioning():
    import asyncio

    from agents.ecs_agent import ECSAgent, aws_infra
    from schemas import ECSDeployRecord, ECSDeployRequest

    req = ECSDeployRequest(project_id="p", cluster="c", service="s", target_group_arn=ARN)
    with pytest.raises(aws_infra.InfraError, match="보안 그룹"):
        asyncio.run(ECSAgent()._step_provision(req, ECSDeployRecord(project_id="p"), {}))


def test_alb_accepts_only_compatible_subnet_zones():
    from aws_infra import InfraError, validate_target_group_network

    ec2, alb = target_network()
    validate_target_group_network(ec2, alb, ARN, ["subnet-a"])
    ec2.describe_subnets.return_value["Subnets"][0]["AvailabilityZone"] = "region-c"
    with pytest.raises(InfraError, match="subnet-a"):
        validate_target_group_network(ec2, alb, ARN, ["subnet-a"])


@pytest.mark.parametrize("change", ["vpc", "missing", "unattached", "instance", "permission"])
def test_alb_network_validation_fails_closed(change):
    from aws_infra import InfraError, validate_target_group_network

    ec2, alb = target_network()
    if change == "vpc":
        ec2.describe_subnets.return_value["Subnets"][0]["VpcId"] = "vpc-other"
    elif change == "missing":
        ec2.describe_subnets.return_value["Subnets"] = []
    elif change == "unattached":
        alb.describe_target_groups.return_value["TargetGroups"][0]["LoadBalancerArns"] = []
    elif change == "instance":
        alb.describe_target_groups.return_value["TargetGroups"][0]["TargetType"] = "instance"
    else:
        alb.describe_load_balancers.side_effect = RuntimeError("AccessDenied")
    with pytest.raises(InfraError):
        validate_target_group_network(ec2, alb, ARN, ["subnet-a"])


@pytest.mark.parametrize("provision", [False, True])
def test_provision_rejects_incompatible_alb_before_build(monkeypatch, provision):
    import asyncio

    from agents.ecs_agent import ECSAgent, aws_infra
    from schemas import ECSDeployRecord, ECSDeployRequest

    ec2, alb = target_network()
    ec2.describe_subnets.return_value["Subnets"][0]["AvailabilityZone"] = "region-c"
    clients = {"ec2": ec2, "elbv2": alb, "ecs": MagicMock(), "logs": MagicMock()}
    for name in ["ensure_cluster", "ensure_log_group"]:
        monkeypatch.setattr(aws_infra, name, lambda *a: None)
    monkeypatch.setattr(
        aws_infra,
        "resolve_subnet_network",
        lambda *a: aws_infra.NetworkTarget(
            vpc_id="vpc-a", subnet_ids=("subnet-a",), internet_routable=True
        ),
    )
    req = ECSDeployRequest(
        project_id="p",
        cluster="c",
        service="s",
        task_definition_family="t",
        ecr_repo="r",
        region="ap-northeast-2",
        target_group_arn=ARN,
        subnet_ids=["subnet-a"],
        security_group_ids=["sg-a"],
        provision=provision,
    )
    with pytest.raises(aws_infra.InfraError, match="subnet-a"):
        asyncio.run(ECSAgent()._step_provision(req, ECSDeployRecord(project_id="p"), clients))
    clients["ecs"].create_service.assert_not_called()
    clients["ecs"].update_service.assert_not_called()
