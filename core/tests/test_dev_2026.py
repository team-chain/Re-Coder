"""2.0.26 — 대규모 방패(ALB + WAF) · AWS 리소스 정리."""
from __future__ import annotations

import asyncio

import pytest

import aws_cleanup
import aws_edge

NOSLEEP = lambda _s: None  # noqa: E731
REGION = "us-east-1"


@pytest.fixture
def aws(monkeypatch, tmp_path):
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws
    monkeypatch.setenv("RECODER_HOME", str(tmp_path))
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield aws_cleanup.make_clients(boto3.session.Session(region_name=REGION))


def _net(c):
    from core import aws_infra
    return aws_infra.discover_default_network(c["ec2"])


def _tags(service="recoder-shop"):
    from core import aws_infra
    return aws_infra.recoder_tags("p1", service)


def _edge(c, **kw):
    net = _net(c)
    args = dict(service="recoder-shop", vpc_id=net.vpc_id, subnet_ids=list(net.subnet_ids), app_port=3000,
                tags=_tags(), sleep=NOSLEEP)
    args.update(kw)
    return aws_edge.ensure_edge(c["ec2"], c["elbv2"], c["wafv2"], **args)


# ── 이름 · 규칙 ───────────────────────────────────────────────────────────

def test_edge_name_is_short_prefixed_and_stable():
    for svc in ("recoder-shop", "My Shop!!", "x" * 80, "recoder-"):
        name = aws_edge.edge_name(svc)
        assert name.startswith("recoder-") and len(name) <= 32 and not name.endswith("-")
        assert name == aws_edge.edge_name(svc)
    assert aws_edge.edge_name("a") != aws_edge.edge_name("b")
    assert "recoder-recoder" not in aws_edge.edge_name("recoder-shop")


def test_waf_rules_rate_limit_and_upload_friendly_common_rules():
    rules = aws_edge.web_acl_rules("recoder-x", 2000, 300)
    assert aws_edge.rate_of(rules) == (2000, 300)
    assert rules[0]["Action"] == {"Block": {}}
    assert rules[0]["Statement"]["RateBasedStatement"]["AggregateKeyType"] == "IP"
    common = rules[1]["Statement"]["ManagedRuleGroupStatement"]
    assert common["Name"] == "AWSManagedRulesCommonRuleSet"
    # 사진 업로드(8KB 넘는 본문)를 막지 않는다 — 기록만
    assert common["RuleActionOverrides"] == [{"Name": "SizeRestrictions_BODY", "ActionToUse": {"Count": {}}}]
    assert rules[2]["Statement"]["ManagedRuleGroupStatement"]["Name"] == "AWSManagedRulesKnownBadInputsRuleSet"
    from core.aws_infra import InfraError
    with pytest.raises(InfraError):
        aws_edge.web_acl_rules("x", 5, 300)
    with pytest.raises(InfraError):
        aws_edge.web_acl_rules("x", 100, 30)


# ── ALB + WAF (moto) ─────────────────────────────────────────────────────

def test_edge_creates_alb_tg_listener_waf_and_locks_tasks_to_the_alb(aws):
    c = aws
    e = _edge(c)
    assert set(e.created) == {"alb", "tg", "listener", "waf"}
    assert e.url.startswith("http://") and e.web_acl_arn
    lb = c["elbv2"].describe_load_balancers(LoadBalancerArns=[e.alb_arn])["LoadBalancers"][0]
    assert lb["Scheme"] == "internet-facing" and len({z["ZoneName"] for z in lb["AvailabilityZones"]}) >= 2
    tg = c["elbv2"].describe_target_groups(TargetGroupArns=[e.target_group_arn])["TargetGroups"][0]
    assert tg["TargetType"] == "ip" and tg["HealthCheckPath"] == "/health" and tg["Port"] == 3000
    tags = c["elbv2"].describe_tags(ResourceArns=[e.alb_arn])["TagDescriptions"][0]["Tags"]
    assert {"Key": "recoder:managed", "Value": "true"} in tags
    task_sg = c["ec2"].describe_security_groups(GroupIds=[e.task_security_group])["SecurityGroups"][0]
    perm = task_sg["IpPermissions"][0]
    assert perm["FromPort"] == 3000 and not perm.get("IpRanges")
    assert perm["UserIdGroupPairs"][0]["GroupId"] == e.alb_security_group
    attached = c["wafv2"].get_web_acl_for_resource(ResourceArn=e.alb_arn)["WebACL"]
    assert attached["Name"] == e.name


def test_edge_is_idempotent_updates_rate_and_turning_waf_off_removes_the_acl(aws):
    c = aws
    first = _edge(c)
    again = _edge(c, rate_limit=500, window=60, health_path="/ready")
    assert again.created == [] and again.alb_arn == first.alb_arn and again.target_group_arn == first.target_group_arn
    acl = aws_edge.find_web_acl(c["wafv2"], first.name)
    rules = c["wafv2"].get_web_acl(Name=first.name, Scope="REGIONAL", Id=acl["Id"])["WebACL"]["Rules"]
    assert aws_edge.rate_of(rules) == (500, 60)
    tg = c["elbv2"].describe_target_groups(TargetGroupArns=[first.target_group_arn])["TargetGroups"][0]
    assert tg["HealthCheckPath"] == "/ready"
    off = _edge(c, waf=False)
    assert off.web_acl_arn == "" and off.notes and aws_edge.find_web_acl(c["wafv2"], first.name) is None


def test_edge_failure_rolls_back_only_what_this_call_created(aws, monkeypatch):
    c = aws

    def broken(*_a, **_k):
        raise RuntimeError("WAF 사용 불가")
    monkeypatch.setattr(aws_edge, "ensure_web_acl", broken)
    from core.aws_infra import InfraError
    with pytest.raises(InfraError) as info:
        _edge(c)
    assert "대규모 방패" in info.value.message
    name = aws_edge.edge_name("recoder-shop")
    assert c["elbv2"].describe_load_balancers()["LoadBalancers"] == []
    assert not [t for t in c["elbv2"].describe_target_groups()["TargetGroups"] if t["TargetGroupName"] == name]

    # 이미 있던 ALB 는 실패해도 남긴다
    monkeypatch.undo()
    keep = _edge(c, waf=False)
    monkeypatch.setattr(aws_edge, "ensure_web_acl", broken)
    with pytest.raises(InfraError):
        _edge(c)
    assert c["elbv2"].describe_load_balancers(LoadBalancerArns=[keep.alb_arn])["LoadBalancers"]


def test_alb_needs_two_zones(aws):
    c = aws
    vpc = c["ec2"].create_vpc(CidrBlock="10.9.0.0/16")["Vpc"]["VpcId"]
    only = c["ec2"].create_subnet(VpcId=vpc, CidrBlock="10.9.1.0/24", AvailabilityZone="us-east-1a")["Subnet"]["SubnetId"]
    from core.aws_infra import InfraError
    with pytest.raises(InfraError) as info:
        aws_edge.alb_subnets(c["ec2"], vpc, [only])
    assert "2곳" in info.value.message


# ── ECS 배포 연결 ────────────────────────────────────────────────────────

def _req(**kw):
    from core.schemas import ECSDeployRequest
    base = dict(project_id="p1", cluster="recoder-cluster", service="recoder-shop", region=REGION,
                container_port=3000, health_check_path="/health")
    base.update(kw)
    return ECSDeployRequest(**base)


def test_edge_request_needs_cost_approval_and_conflicts_are_rejected_before_aws():
    from core.agents.ecs_agent import ECSAgent
    from core.aws_infra import InfraError
    ECSAgent._check_edge_request(_req())   # 끔 — 아무 조건 없음
    with pytest.raises(InfraError) as info:
        ECSAgent._check_edge_request(_req(edge="alb_waf"))
    assert "비용" in info.value.message and "월" in info.value.remedy
    tg = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/mine/0123456789abcdef"
    with pytest.raises(InfraError):
        ECSAgent._check_edge_request(_req(edge="alb", edge_cost_approved=True, target_group_arn=tg))
    with pytest.raises(InfraError):
        ECSAgent._check_edge_request(_req(edge="alb", edge_cost_approved=True, provision=False,
                                          subnet_ids=["subnet-1"], security_group_ids=["sg-1"]))
    ECSAgent._check_edge_request(_req(edge="alb_waf", edge_cost_approved=True))


def test_provision_with_edge_uses_alb_target_group_and_alb_only_security_group(aws):
    from core.agents.ecs_agent import ECSAgent
    from core.schemas import ECSDeployRecord
    agent = ECSAgent()
    req = _req(edge="alb_waf", edge_cost_approved=True, waf_rate_limit=1000)
    rec = ECSDeployRecord(project_id="p1", cluster=req.cluster, service=req.service, region=REGION)
    asyncio.run(agent._step_provision(req, rec, aws))
    assert req.target_group_arn.startswith("arn:aws:elasticloadbalancing:")
    edge = agent._edge[id(req)]
    assert req.security_group_ids == [edge.task_security_group]
    assert "http://" in rec.provisioned["load_balancer"] and "1,000회" in rec.provisioned["waf"]
    # 공개 그룹({service}-sg)은 만들지 않는다
    names = {g["GroupName"] for g in aws["ec2"].describe_security_groups()["SecurityGroups"]}
    assert "recoder-shop-sg" not in names and "recoder-shop-task-sg" in names
    # 방패(nginx)는 ALB 가 넘긴 실제 IP 를 쓴다
    from core import shield
    d = shield.decide(enabled=True, level="normal", public_port=3000, port_from_env=True)
    td = agent._render_task_definition(req, image="img", execution_role_arn="arn:aws:iam::123456789012:role/x",
                                       task_role_arn="", shield_decision=d)
    import base64
    env = {e["name"]: e["value"] for e in td["containerDefinitions"][1]["environment"]}
    conf = base64.b64decode(env["RECODER_SHIELD_CONF_B64"]).decode()
    assert "real_ip_header X-Forwarded-For" in conf and "$http_x_forwarded_proto" in conf


def test_failed_deploy_with_alb_warns_about_hourly_cost():
    from core.agents.ecs_agent import ECSAgent
    from core.schemas import ECSDeployRecord
    rec = ECSDeployRecord(project_id="p1", cluster="c", service="s", region=REGION)
    rec.provisioned["load_balancer"] = "recoder-s-1234 — http://x"
    ECSAgent._warn_if_resources_may_be_running(_req(), rec)
    assert "AWS 리소스 정리" in rec.provisioned["edge_cost_warning"]


def test_extension_request_passes_edge_options():
    from api.routes.deploy_ecs import ExtensionEcsDeployRequest
    fields = ExtensionEcsDeployRequest.model_fields
    for name in ("edge", "waf_rate_limit", "waf_window", "edge_cost_approved"):
        assert name in fields


# ── 리소스 정리 (moto) ───────────────────────────────────────────────────

def _deploy_world(c, *, waf=True):
    """ReCoder 가 만든 것(태그) + 옛날 것(태그 없음) + 남의 것."""
    from core import aws_infra
    tags = _tags()
    e = _edge(c, waf=waf)
    aws_infra.ensure_cluster(c["ecs"], "recoder-cluster", tags=tags)
    aws_infra.ensure_log_group(c["logs"], "/ecs/recoder-shop", tags=tags)
    aws_infra.ensure_ecr_repository(c["ecr"], "recoder-shop", tags=tags)
    td = c["ecs"].register_task_definition(
        family="recoder-shop", networkMode="awsvpc", requiresCompatibilities=["FARGATE"], cpu="256", memory="512",
        containerDefinitions=[{"name": "app", "image": "x", "memory": 512}])["taskDefinition"]["taskDefinitionArn"]
    aws_infra.tag_created(c["ecs"], "ecs", td, tags)
    net = _net(c)
    aws_infra.ensure_service(c["ecs"], cluster="recoder-cluster", service="recoder-shop", task_definition=td,
                             subnet_ids=net.subnet_ids, security_group_ids=[e.task_security_group], tags=tags,
                             target_group_arn=e.target_group_arn, container_name="app", container_port=3000)
    # 옛날 배포(태그 없음)와 남의 리소스
    c["ecr"].create_repository(repositoryName="recoder-old")
    c["ecr"].create_repository(repositoryName="company-api")
    c["logs"].create_log_group(logGroupName="/ecs/company-api")
    c["s3"].create_bucket(Bucket="recoder-site-1")
    c["s3"].put_bucket_tagging(Bucket="recoder-site-1", Tagging={"TagSet": [{"Key": "recoder:managed", "Value": "true"}]})
    c["s3"].put_object(Bucket="recoder-site-1", Key="index.html", Body=b"<h1>x</h1>")
    c["s3"].create_bucket(Bucket="recoder-old-site")
    return e


def _ids(plan):
    return {i["id"] for g in plan["groups"] for i in g["items"]}


def test_plan_groups_tagged_items_and_lists_untagged_as_manual(aws):
    _deploy_world(aws)
    plan = aws_cleanup.plan(aws, region=REGION)
    assert not plan["gaps"]
    ids = _ids(plan)
    for want in ("ecs_service:recoder-cluster/recoder-shop", "task_definitions:recoder-shop", "ecr:recoder-shop",
                 "log_group:/ecs/recoder-shop", "ecs_cluster:recoder-cluster", "s3_bucket:recoder-site-1"):
        assert want in ids
    assert any(i.startswith("alb:") for i in ids) and any(i.startswith("waf:") for i in ids)
    assert sum(1 for i in ids if i.startswith("security_group:")) == 2
    untagged = {i["id"]: i for i in plan["untagged"]}
    assert set(untagged) == {"ecr:recoder-old", "s3_bucket:recoder-old-site"}
    assert all(not i["deletable"] and "직접" in i["note"] for i in untagged.values())
    everything = ids | set(untagged)
    assert "ecr:company-api" not in everything and "log_group:/ecs/company-api" not in everything
    assert plan["summary"]["load_balancers"] == 1 and plan["summary"]["web_acls"] == 1


def test_delete_requires_approval_and_never_touches_untagged(aws):
    _deploy_world(aws)
    with pytest.raises(PermissionError):
        aws_cleanup.delete(aws, ["ecr:recoder-shop"], region=REGION, approved=False)
    out = aws_cleanup.delete(aws, ["ecr:recoder-old", "ecr:company-api", "s3_bucket:recoder-old-site"],
                             region=REGION, approved=True, sleep=NOSLEEP)
    assert out["deleted"] == 0 and all(r.get("skipped") for r in out["results"])
    names = {r["repositoryName"] for r in aws["ecr"].describe_repositories()["repositories"]}
    assert {"recoder-old", "company-api"} <= names
    assert "recoder-old-site" in {b["Name"] for b in aws["s3"].list_buckets()["Buckets"]}


def test_delete_everything_tagged_in_dependency_order_leaves_nothing(aws):
    _deploy_world(aws)
    plan = aws_cleanup.plan(aws, region=REGION)
    out = aws_cleanup.delete(aws, sorted(_ids(plan)), region=REGION, approved=True, sleep=NOSLEEP)
    assert out["failed"] == 0, out["results"]
    order = [r["id"].split(":")[0] for r in out["results"] if r["ok"]]
    assert order.index("ecs_service") < order.index("waf") < order.index("alb") < order.index("target_group")
    assert order.index("target_group") < order.index("security_group") < order.index("ecs_cluster")
    after = aws_cleanup.plan(aws, region=REGION)
    assert _ids(after) == set()
    assert {i["id"] for i in after["untagged"]} == {"ecr:recoder-old", "s3_bucket:recoder-old-site"}
    assert aws["elbv2"].describe_load_balancers()["LoadBalancers"] == []


def test_task_sg_is_deleted_before_the_alb_sg_it_references():
    alb = aws_cleanup.Item(id="security_group:sg-a", kind="security_group", name="alb", arn="sg-a", tagged=True)
    task = aws_cleanup.Item(id="security_group:sg-t", kind="security_group", name="task", arn="sg-t", tagged=True,
                            extra={"refs": ["sg-a"]})
    assert [g.arn for g in aws_cleanup._sg_order([alb, task])] == ["sg-t", "sg-a"]


def test_cluster_with_someone_elses_service_is_kept(aws):
    from core import aws_infra
    _deploy_world(aws, waf=False)
    net = _net(aws)
    td = aws["ecs"].register_task_definition(family="other", containerDefinitions=[{"name": "a", "image": "x", "memory": 64}],
                                             networkMode="awsvpc")["taskDefinition"]["taskDefinitionArn"]
    aws_infra.ensure_service(aws["ecs"], cluster="recoder-cluster", service="hand-made", task_definition=td,
                             subnet_ids=net.subnet_ids,
                             security_group_ids=[aws["ec2"].create_security_group(
                                 GroupName="hand", Description="hand", VpcId=net.vpc_id)["GroupId"]])
    out = aws_cleanup.delete(aws, ["ecs_service:recoder-cluster/recoder-shop", "ecs_cluster:recoder-cluster"],
                             region=REGION, approved=True, sleep=NOSLEEP)
    by = {r["id"]: r for r in out["results"]}
    assert by["ecs_service:recoder-cluster/recoder-shop"]["ok"]
    assert by["ecs_cluster:recoder-cluster"].get("skipped") and "hand-made" in by["ecs_cluster:recoder-cluster"]["message"]


def test_stop_scales_to_zero_and_stops_watching(aws):
    import ops_alerts
    _deploy_world(aws, waf=False)
    ops_alerts.register(region=REGION, cluster="recoder-cluster", service="recoder-shop", log_group="/ecs/recoder-shop")
    out = aws_cleanup.stop(aws, region=REGION, cluster="recoder-cluster", service="recoder-shop")
    assert out["ok"]
    svc = aws["ecs"].describe_services(cluster="recoder-cluster", services=["recoder-shop"])["services"][0]
    assert svc["desiredCount"] == 0
    assert ops_alerts.monitors() == []


def test_one_denied_kind_is_reported_not_fatal(aws):
    _deploy_world(aws, waf=False)

    class Denied:
        def __getattr__(self, name):
            def fail(*_a, **_k):
                from botocore.exceptions import ClientError
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, name)
            return fail
    clients = dict(aws, wafv2=Denied())
    plan = aws_cleanup.plan(clients, region=REGION)
    assert any("로드밸런서·WAF" in g for g in plan["gaps"])
    assert "ecr:recoder-shop" in _ids(plan)


def test_cleanup_routes(aws, monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import aws_cleanup as routes
    monkeypatch.setattr(routes, "clients_for", lambda region: aws)
    monkeypatch.setenv("RECODER_ECS_STORE", str(tmp_path / "none.json"))
    _deploy_world(aws, waf=False)
    app = FastAPI()
    app.include_router(routes.router)
    c = TestClient(app)
    plan = c.post("/api/aws/cleanup/plan", json={"region": REGION}).json()
    assert "ecr:recoder-shop" in _ids(plan)
    assert c.post("/api/aws/cleanup/plan", json={"region": "nowhere"}).status_code == 422
    r = c.post("/api/aws/cleanup/delete", json={"region": REGION, "ids": ["ecr:recoder-shop"], "approved": False})
    assert r.status_code == 400
    r = c.post("/api/aws/cleanup/delete", json={"region": REGION, "ids": ["ecr:recoder-shop"], "approved": True, "count": 2})
    assert r.status_code == 400
    r = c.post("/api/aws/cleanup/delete", json={"region": REGION, "ids": ["ecr:recoder-shop"], "approved": True, "count": 1})
    assert r.status_code == 200 and r.json()["deleted"] == 1
    r = c.post("/api/aws/cleanup/stop", json={"region": REGION, "cluster": "recoder-cluster", "service": "recoder-shop"})
    assert r.status_code == 200 and r.json()["ok"]


def test_deploy_records_bring_untagged_old_services_into_view(aws, monkeypatch, tmp_path):
    import json
    from api.routes import aws_cleanup as routes
    store = tmp_path / "ecs.json"
    store.write_text(json.dumps([{"cluster": "legacy", "service": "shop", "region": REGION, "started_at": "x"}]),
                     encoding="utf-8")
    monkeypatch.setenv("RECODER_ECS_STORE", str(store))
    assert routes._records(REGION) == [{"cluster": "legacy", "service": "shop", "region": REGION}]
    aws["ecs"].create_cluster(clusterName="legacy")
    net = _net(aws)
    td = aws["ecs"].register_task_definition(family="shop", containerDefinitions=[{"name": "a", "image": "x", "memory": 64}],
                                             networkMode="awsvpc")["taskDefinition"]["taskDefinitionArn"]
    aws["ecs"].create_service(cluster="legacy", serviceName="shop", taskDefinition=td, desiredCount=1,
                              networkConfiguration={"awsvpcConfiguration": {
                                  "subnets": list(net.subnet_ids),
                                  "securityGroups": [aws["ec2"].create_security_group(
                                      GroupName="legacy", Description="legacy", VpcId=net.vpc_id)["GroupId"]]}})
    plan = aws_cleanup.plan(aws, region=REGION, records=routes._records(REGION))
    untagged = {i["id"] for i in plan["untagged"]}
    assert "ecs_service:legacy/shop" in untagged and "ecs_cluster:legacy" in untagged


def test_url_check_uses_the_alb_address_and_waits_longer(monkeypatch):
    from core.agents import ecs_agent as mod
    from core.schemas import ECSDeployRecord
    seen = {}

    def probe(url, **kw):
        seen.update(url=url, **kw)
        return True, "HTTP 200"
    monkeypatch.setattr(mod, "_probe_http", probe)
    agent = mod.ECSAgent()
    req = _req(edge="alb", edge_cost_approved=True)
    agent._edge[id(req)] = aws_edge.EdgeResult(name="recoder-shop-1", alb_arn="a", dns_name="lb.example.com",
                                               target_group_arn="t", listener_arn="l", alb_security_group="sa",
                                               task_security_group="st")
    rec = ECSDeployRecord(project_id="p1", cluster=req.cluster, service=req.service, region=REGION)
    asyncio.run(agent._step_resolve_url(req, rec, {}))
    assert rec.service_url == "http://lb.example.com"
    assert seen["url"] == "http://lb.example.com/health" and seen["attempts"] == 30 and seen["interval"] == 10.0
