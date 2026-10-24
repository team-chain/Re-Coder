"""3.0.2 — ReCoder 가 만들지 않은 ECR 리포지토리·보안 그룹을 건드리지 않는다 (moto)."""
from __future__ import annotations

import json

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

import aws_infra  # noqa: E402

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _fake_creds(monkeypatch):
    for k in ("AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


def _policy(ecr, name):
    try:
        return json.loads(ecr.get_lifecycle_policy(repositoryName=name)["lifecyclePolicyText"])
    except ecr.exceptions.LifecyclePolicyNotFoundException:
        return None


@mock_aws
def test_lifecycle_policy_is_not_overwritten_on_a_users_repository():
    ecr = boto3.client("ecr", region_name=REGION)
    ecr.create_repository(repositoryName="company-api")
    mine = {"rules": [{"rulePriority": 1, "description": "keep 500", "selection": {
        "tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 500}, "action": {"type": "expire"}}]}
    ecr.put_lifecycle_policy(repositoryName="company-api", lifecyclePolicyText=json.dumps(mine))
    aws_infra.ensure_ecr_repository(ecr, "company-api", tags=aws_infra.recoder_tags("p", "company-api"))
    assert _policy(ecr, "company-api") == mine, "사용자 리포지토리의 정책을 덮어쓰면 그쪽 이미지가 지워진다"
    tags = ecr.list_tags_for_resource(resourceArn=ecr.describe_repositories(repositoryNames=["company-api"])
                                      ["repositories"][0]["repositoryArn"])["tags"]
    assert not any(t["Key"] == aws_infra.MANAGED_TAG for t in tags), "재사용한 리포지토리에 태그를 붙이지 않는다"


@mock_aws
def test_lifecycle_policy_on_created_and_managed_repositories_keeps_ten():
    ecr = boto3.client("ecr", region_name=REGION)
    aws_infra.ensure_ecr_repository(ecr, "recoder-new")  # 태그 없이 불러도 만든 것은 표시한다
    rule = _policy(ecr, "recoder-new")["rules"][0]
    assert rule["selection"]["countNumber"] == 10 == aws_infra.DEFAULT_ECR_KEEP_LAST
    arn = ecr.describe_repositories(repositoryNames=["recoder-new"])["repositories"][0]["repositoryArn"]
    assert {"Key": aws_infra.MANAGED_TAG, "Value": "true"} in ecr.list_tags_for_resource(resourceArn=arn)["tags"]
    # 재사용 — 태그가 있으니 정책을 다시 건다
    ecr.delete_lifecycle_policy(repositoryName="recoder-new")
    aws_infra.ensure_ecr_repository(ecr, "recoder-new")
    assert _policy(ecr, "recoder-new")["rules"][0]["selection"]["countNumber"] == 10


def _vpc(ec2) -> str:
    return aws_infra.discover_default_network(ec2).vpc_id


@mock_aws
def test_untagged_security_group_with_the_same_name_is_refused():
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc = _vpc(ec2)
    gid = ec2.create_security_group(GroupName="shop-sg", Description="team db access", VpcId=vpc)["GroupId"]
    with pytest.raises(aws_infra.InfraError) as err:
        aws_infra.ensure_security_group(ec2, vpc_id=vpc, name="shop-sg", port=8000,
                                        tags=aws_infra.recoder_tags("p", "shop"), require_managed=True)
    assert "security_group_ids" in err.value.remedy and gid in err.value.remedy
    perms = ec2.describe_security_groups(GroupIds=[gid])["SecurityGroups"][0]["IpPermissions"]
    assert perms == [], "남의 그룹에 0.0.0.0/0 규칙을 더하지 않는다"


@mock_aws
def test_created_group_is_tagged_reused_and_old_public_port_is_revoked():
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc = _vpc(ec2)
    gid = aws_infra.ensure_security_group(ec2, vpc_id=vpc, name="shop-sg", port=8000,
                                          tags=aws_infra.recoder_tags("p", "shop"), require_managed=True)
    group = ec2.describe_security_groups(GroupIds=[gid])["SecurityGroups"][0]
    assert {"Key": aws_infra.MANAGED_TAG, "Value": "true"} in group["Tags"]
    # 사용자가 손으로 넣은 규칙(설명이 다름)은 건드리지 않는다
    ec2.authorize_security_group_ingress(GroupId=gid, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "ssh"}]}])
    again = aws_infra.ensure_security_group(ec2, vpc_id=vpc, name="shop-sg", port=9000,
                                            tags=aws_infra.recoder_tags("p", "shop"), require_managed=True)
    assert again == gid
    ports = sorted(p["FromPort"] for p in ec2.describe_security_groups(GroupIds=[gid])["SecurityGroups"][0]["IpPermissions"])
    assert ports == [22, 9000], f"옛 앱 포트 8000 이 계속 열려 있다: {ports}"
