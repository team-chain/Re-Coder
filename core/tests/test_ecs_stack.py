"""4.0.1 다중 서비스 AWS 배포 — compose 서비스마다 ECS 서비스, Service Connect 로 compose 와 같은 이름으로 연결."""
import asyncio
import json
from pathlib import Path

import pytest

import ecs_stack

COMPOSE = """name: app
services:
  users-db:
    image: postgres:16-alpine
    volumes: [users-db-data:/var/lib/postgresql/data]
    environment:
      POSTGRES_DB: users_db
      POSTGRES_USER: app
      POSTGRES_PASSWORD: ${USERS_DB_PASSWORD:?set USERS_DB_PASSWORD in .env}
    healthcheck:
      test: [CMD-SHELL, pg_isready -U app -d users_db]
      interval: 5s
      timeout: 5s
      retries: 30
      start_period: 10s
  redis:
    image: redis:7-alpine
    volumes: [redis-data:/data]
    environment:
      REDIS_PASSWORD: ${REDIS_PASSWORD:?set}
    command: [sh, -c, 'exec redis-server --requirepass "$$REDIS_PASSWORD"']
  web:
    build: {context: ./services/web}
    environment:
      GATEWAY_URL: http://gateway:8080
    depends_on: {gateway: {condition: service_healthy}}
    healthcheck:
      test: [CMD, wget, -q, --spider, http://127.0.0.1:3000/]
    ports: ["${RECODER_LOCAL_BIND:-127.0.0.1}:3000:3000"]
  gateway:
    build: {context: ./services/gateway}
    environment:
      PORT: '8080'
      USERS_URL: http://users:3001
    depends_on: {users: {condition: service_healthy}}
    ports: ["${RECODER_LOCAL_BIND:-127.0.0.1}:8080:8080"]
  users:
    build: {context: ./services/users}
    environment:
      PORT: '3001'
      USERS_DB_URL: postgres://app:${USERS_DB_PASSWORD}@users-db:5432/users_db
    depends_on: {users-db: {condition: service_healthy}}
  worker:
    build: {context: ./services/worker}
    environment:
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
    depends_on: [redis]
volumes: {users-db-data: {}, redis-data: {}}
"""
ENV = {"USERS_DB_PASSWORD": "pw-1", "REDIS_PASSWORD": "pw-2"}


def _ws(tmp_path):
    ws = tmp_path / "shop"
    for svc in ("web", "gateway", "users", "worker"):
        (ws / "services" / svc).mkdir(parents=True)
        (ws / "services" / svc / "Dockerfile").write_text("FROM node:22-alpine\n", encoding="utf-8")
    (ws / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    return ws


def test_plan_reads_services_ports_order_and_one_entry(tmp_path):
    p = ecs_stack.plan(_ws(tmp_path), ENV)
    assert not p.errors, p.errors
    by = {s.name: s for s in p.services}
    assert by["users-db"].port == 5432 and by["redis"].port == 6379 and by["users"].port == 3001
    assert by["gateway"].port == 8080 and by["web"].port == 3000 and by["worker"].port is None
    assert [s.name for s in p.services if s.public] == ["web"], "입구는 화면 하나 — gateway 는 web 의 nginx 를 거친다"
    assert by["users"].environment["USERS_DB_URL"] == "postgres://app:pw-1@users-db:5432/users_db"
    assert by["redis"].command == ["sh", "-c", 'exec redis-server --requirepass "$REDIS_PASSWORD"']
    assert by["users-db"].health["retries"] == 10 and by["users-db"].health["interval"] == 5
    flat = [n for layer in p.layers for n in layer]
    assert flat.index("users-db") < flat.index("users") < flat.index("gateway") < flat.index("web")
    assert flat.index("redis") < flat.index("worker")
    assert p.monthly_estimate() > 0 and any("데이터가 사라집니다" in n for n in p.notes)


def test_missing_secret_is_an_error_not_an_empty_password(tmp_path):
    p = ecs_stack.plan(_ws(tmp_path), {"REDIS_PASSWORD": "x"})
    assert any("USERS_DB_PASSWORD" in e for e in p.errors)


def test_task_definition_and_service_connect_keep_compose_names(tmp_path):
    p = ecs_stack.plan(_ws(tmp_path), ENV)
    users = next(s for s in p.services if s.name == "users")
    td = ecs_stack.task_definition(users, project="recoder-shop-1", image="1.dkr.ecr/x:1", execution_role="arn:exec",
                                   task_role="", log_group="/ecs/recoder-shop-1", region="us-east-1")
    c = td["containerDefinitions"][0]
    assert td["family"] == "recoder-shop-1-users" and td["networkMode"] == "awsvpc" and "taskRoleArn" not in td
    assert c["portMappings"] == [{"name": "users-3001", "containerPort": 3001, "protocol": "tcp", "appProtocol": "http"}]
    sc = ecs_stack.service_connect(users, "recoder-shop-1")
    assert sc["services"][0]["clientAliases"] == [{"port": 3001, "dnsName": "users"}]
    worker = next(s for s in p.services if s.name == "worker")
    assert ecs_stack.service_connect(worker, "ns") == {"enabled": True, "namespace": "ns"}
    db = next(s for s in p.services if s.name == "users-db")
    assert "appProtocol" not in ecs_stack.task_definition(db, project="p", image=db.image, execution_role="e", task_role="",
                                                          log_group="g", region="r")["containerDefinitions"][0]["portMappings"][0]


def test_wait_stable_reports_failures_and_timeouts():
    class E:
        def __init__(self): self.n = 0
        def describe_services(self, cluster, services):
            self.n += 1
            return {"services": [
                {"serviceName": "a", "runningCount": 1, "deployments": [{"status": "PRIMARY", "rolloutState": "COMPLETED"}]},
                {"serviceName": "b", "runningCount": 0, "deployments": [{"status": "PRIMARY", "rolloutState": "FAILED", "rolloutStateReason": "circuit breaker"}]}]}
    assert ecs_stack.wait_stable(E(), cluster="c", names=["a", "b"], sleep=lambda s: None) == {"a": "stable", "b": "failed: circuit breaker"}
    t = iter([0, 0, 1000, 1000])
    class Slow:
        def describe_services(self, cluster, services):
            return {"services": [{"serviceName": "a", "runningCount": 0, "deployments": [{"status": "PRIMARY", "rolloutState": "IN_PROGRESS"}]}]}
    out = ecs_stack.wait_stable(Slow(), cluster="c", names=["a"], timeout=10, sleep=lambda s: None, monotonic=lambda: next(t))
    assert out["a"].startswith("failed")


def test_agent_deploys_every_service_in_order(tmp_path, monkeypatch):
    from agents import ecs_agent as agent_mod
    from core.agents import ecs_build
    from core import aws_infra
    from schemas import ECSDeployRequest, ECSDeployRecord, ECSDeployStatus
    import compose_deploy

    ws = _ws(tmp_path)
    (ws / ".env").write_text("USERS_DB_PASSWORD=pw-1\nREDIS_PASSWORD=pw-2\n", encoding="utf-8")
    monkeypatch.setattr(compose_deploy, "load", lambda w: None)
    calls = []

    class ECS:
        def __init__(self): self.created = []
        def describe_clusters(self, clusters): return {"clusters": [{"status": "ACTIVE", "clusterArn": "arn:c"}]}
        def update_cluster(self, **kw): calls.append(("namespace", kw["serviceConnectDefaults"]["namespace"]))
        def register_task_definition(self, **td):
            calls.append(("td", td["family"]))
            return {"taskDefinition": {"taskDefinitionArn": "arn:td/" + td["family"]}}
        def describe_services(self, cluster, services):
            return {"services": [{"serviceName": n, "status": "ACTIVE", "runningCount": 1,
                                  "deployments": [{"status": "PRIMARY", "rolloutState": "COMPLETED"}]} for n in services if n in self.created]}
        def create_service(self, **kw):
            self.created.append(kw["serviceName"]); calls.append(("svc", kw["serviceName"], kw["serviceConnectConfiguration"],
                                                                 kw["networkConfiguration"]["awsvpcConfiguration"]["securityGroups"]))
            return {"service": {"serviceArn": "arn:s/" + kw["serviceName"]}}

    class EC2:
        def describe_security_groups(self, Filters): return {"SecurityGroups": []}
        def create_security_group(self, **kw): return {"GroupId": "sg-internal"}
        def authorize_security_group_ingress(self, **kw): calls.append(("ingress", kw["IpPermissions"][0]["UserIdGroupPairs"]))

    ecs = ECS()
    monkeypatch.setattr(agent_mod.ECSAgent, "_clients", staticmethod(lambda region: {"ecs": ecs, "ec2": EC2(), "logs": None, "ecr": None, "sts": None}))
    monkeypatch.setattr(agent_mod.ECSAgent, "_resolve_role_arns", lambda self, req, clients: ("arn:exec", ""))
    monkeypatch.setattr(aws_infra, "ensure_cluster", lambda *a, **k: "arn:c")
    monkeypatch.setattr(aws_infra, "ensure_log_group", lambda *a, **k: "g")
    monkeypatch.setattr(aws_infra, "discover_default_network", lambda ec2: aws_infra.NetworkTarget(vpc_id="vpc-1", subnet_ids=("s-1",)))
    monkeypatch.setattr(aws_infra, "ensure_security_group", lambda *a, **k: "sg-public")
    monkeypatch.setattr(aws_infra, "ensure_ecr_repository", lambda ecr, name, **k: f"1.dkr.ecr/{name}")
    monkeypatch.setattr(aws_infra, "wait_for_public_url", lambda *a, **k: "http://3.3.3.3:3000")
    monkeypatch.setattr(ecs_build, "build_and_push", lambda ecr, **k: calls.append(("build", Path(k["workspace_path"]).name))
                        or type("R", (), {"image_uri": k["repository_uri"] + ":" + k["tag"], "digest": "sha"})())
    monkeypatch.setattr(ecs_stack, "wait_stable", lambda ecs, cluster, names, **k: {n: "stable" for n in names})
    import ops_alerts
    monitors = []
    monkeypatch.setattr(ops_alerts, "register", lambda **kw: monitors.append(kw) or kw)

    req = ECSDeployRequest(project_id="p", cluster="recoder-cluster", service="recoder-app", region="us-east-1", workspace_path=str(ws))
    rec = ECSDeployRecord()
    out = asyncio.run(agent_mod.ECSAgent()._deploy_pipeline(req, rec))
    assert out.status == ECSDeployStatus.SUCCEEDED, (out.error_message, out.error_detail)
    assert out.service_url == "http://3.3.3.3:3000" and out.service.endswith("-web")
    assert sorted(c[1] for c in calls if c[0] == "build") == ["gateway", "users", "web", "worker"]
    svcs = [c for c in calls if c[0] == "svc"]
    order = [c[1].rsplit("-", 1)[-1] if not c[1].endswith("users-db") else "users-db" for c in svcs]
    assert order.index("users-db") < order.index("users") < order.index("gateway") < order.index("web")
    web = next(c for c in svcs if c[1].endswith("-web"))
    assert web[3] == ["sg-internal", "sg-public"] and all(c[3] == ["sg-internal"] for c in svcs if c is not web)
    assert ("ingress", [{"GroupId": "sg-internal"}]) in calls
    assert "월 약 $" in out.provisioned["stack_cost"] and "web" in out.provisioned["stack_entry"]
    # Operate(모니터)에 서비스마다 올라가고, 입구(web)가 마지막 — 화면이 기본으로 입구를 보여 준다.
    assert len(monitors) == len([c for c in calls if c[0] == "svc"])
    assert monitors[-1]["service"].endswith("-web") and all(m["metrics"] is False for m in monitors)
    assert "Operate" in out.provisioned["alerts"]


def test_internal_group_is_made_even_without_tag_permission():
    """옛 권한표(ec2:CreateTags 없음)라도 서비스 사이 보안 그룹을 만들고, 다음 배포에서 다시 쓴다."""
    class Denied(Exception):
        response = {"Error": {"Code": "UnauthorizedOperation"}}

    made = []

    class EC2:
        groups: list = []
        def describe_security_groups(self, Filters): return {"SecurityGroups": self.groups}
        def create_security_group(self, **kw):
            if "TagSpecifications" in kw:
                raise Denied("no CreateTags")
            made.append(kw); return {"GroupId": "sg-1"}
        def create_tags(self, **kw): raise Denied("no CreateTags")
        def authorize_security_group_ingress(self, **kw): pass

    ec2 = EC2()
    assert ecs_stack.ensure_internal_group(ec2, vpc_id="v", name="p-internal", tags={"recoder:managed": "true"}) == "sg-1"
    assert made and made[0]["Description"] == ecs_stack.INTERNAL_GROUP_DESCRIPTION
    ec2.groups = [{"GroupId": "sg-1", "Description": ecs_stack.INTERNAL_GROUP_DESCRIPTION, "Tags": []}]
    assert ecs_stack.ensure_internal_group(ec2, vpc_id="v", name="p-internal", tags={}) == "sg-1"
    ec2.groups = [{"GroupId": "sg-9", "Description": "someone else", "Tags": []}]
    import pytest
    with pytest.raises(RuntimeError):
        ecs_stack.ensure_internal_group(ec2, vpc_id="v", name="p-internal", tags={})
