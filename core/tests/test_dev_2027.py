"""2.0.27 — HTTPS 주소(CloudFront + 내 도메인) · 관리형 DB(RDS)."""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

import aws_cleanup
from core import aws_cdn, aws_rds, shield
from core.agents import ecs_runtime

NOSLEEP = lambda _s: None  # noqa: E731
REGION = "us-east-1"
SERVER = """import express from 'express';
import pg from 'pg';
const app = express();
if (!process.env.DATABASE_URL) { console.error('DATABASE_URL required'); process.exit(1); }
const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });
app.get('/health', (_req, res) => res.json({ ok: true }));
app.listen(process.env.PORT || 3000);
"""


def _app(tmp_path):
    files = {
        "package.json": json.dumps({"name": "shop", "type": "module", "dependencies": {"express": "^4.19.2", "pg": "^8.11.0"}}),
        "src/server.js": SERVER,
        "schema.sql": "CREATE TABLE IF NOT EXISTS products (id SERIAL PRIMARY KEY, name TEXT);\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def _runtime(tmp_path, environment="staging", managed_db=True, monkeypatch=None):
    return ecs_runtime.plan(str(tmp_path), key="us-east-1-c-s", environment=environment, provided=set(),
                            container_port=3000, managed_db=managed_db)


@pytest.fixture(autouse=True)
def _runtime_store(tmp_path, monkeypatch):
    monkeypatch.setenv("RECODER_ECS_RUNTIME_DIR", str(tmp_path / "runtime-store"))


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
        session = boto3.session.Session(region_name=REGION)
        clients = aws_cleanup.make_clients(session)
        clients["route53"] = session.client("route53")
        clients["sts"] = session.client("sts")
        yield clients


def _req(**kw):
    from core.schemas import ECSDeployRequest
    base = dict(project_id="p1", cluster="recoder-cluster", service="recoder-shop", region=REGION,
                container_port=3000, health_check_path="/health")
    base.update(kw)
    return ECSDeployRequest(**base)


# ── 실행 설정 ────────────────────────────────────────────────────────────

def test_managed_db_replaces_the_task_postgres_and_keeps_init_sql(tmp_path):
    staging = _runtime(_app(tmp_path))
    assert not staging.blocked
    assert "postgres" not in staging.services and not any(c["name"] == "recoder-postgres" for c in staging.sidecars)
    assert "DATABASE_URL" in staging.managed_db["names"]
    assert base64.b64decode(staging.managed_db["init_sql_b64"]).decode().startswith("CREATE TABLE")
    assert any("관리형 DB" in n for n in staging.notes)
    # production 도 DATABASE_URL 때문에 막히지 않는다(RDS 가 채운다)
    prod = _runtime(tmp_path, environment="production")
    assert not any(m["name"] == "DATABASE_URL" for m in prod.missing)
    # 끄면 예전처럼 태스크 안 임시 DB
    assert "postgres" in _runtime(tmp_path, managed_db=False).services


def test_request_checks_for_https_domain_and_rds():
    from core.agents.ecs_agent import ECSAgent
    from core.aws_infra import InfraError
    req = _req(https=True, edge_cost_approved=True)
    ECSAgent._check_edge_request(req)
    assert req.edge == "alb"                              # HTTPS 는 로드밸런서 앞에 둔다
    with pytest.raises(InfraError):
        ECSAgent._check_edge_request(_req(https=True))     # 로드밸런서 비용 확인 필요
    with pytest.raises(InfraError):
        ECSAgent._check_edge_request(_req(custom_domain="shop.example.com"))
    with pytest.raises(InfraError) as info:
        ECSAgent._check_edge_request(_req(database="rds"))
    assert "RDS" in info.value.message and "월" in info.value.remedy
    ECSAgent._check_edge_request(_req(database="rds", db_cost_approved=True))


# ── 방패: CloudFront 뒤 ──────────────────────────────────────────────────

def test_shield_behind_cloudfront_counts_viewer_ip_and_keeps_it_after_reconfigure():
    d = shield.decide(enabled=True, level="normal", public_port=3000, port_from_env=True)
    td = {"containerDefinitions": [{"name": "app", "image": "x", "portMappings": [{"containerPort": 3000}]}], "memory": "512"}
    td = shield.apply(td, "app", d, trust_vpc_proxy=True, trust_cdn=True)
    assert shield.current_settings(td)["proxy"] == "cdn"
    new = shield.reconfigure(td, deny_add=["8.8.8.8"])
    env = {e["name"]: e["value"] for e in new["containerDefinitions"][-1]["environment"]}
    conf = base64.b64decode(env["RECODER_SHIELD_CONF_B64"]).decode()
    assert "$http_cloudfront_viewer_address" in conf and "limit_req_zone $recoder_client" in conf
    assert "geo $recoder_client $recoder_denied" in conf and "8.8.8.8 1;" in conf and "deny 8.8.8.8;" not in conf
    assert "$http_cloudfront_forwarded_proto" in conf
    # ALB 만(2.0.26) 이던 태스크 정의도 대응 뒤에 ALB 뒤라는 것을 잊지 않는다(2.0.26 결함)
    alb = shield.apply({"containerDefinitions": [{"name": "app", "image": "x"}], "memory": "512"}, "app", d,
                       trust_vpc_proxy=True)
    again = shield.reconfigure(alb, level="strict")
    conf = base64.b64decode({e["name"]: e["value"] for e in again["containerDefinitions"][-1]["environment"]}
                            ["RECODER_SHIELD_CONF_B64"]).decode()
    assert "real_ip_header X-Forwarded-For" in conf and "rate=5r/s" in conf


# ── 배포 연결 (moto) ─────────────────────────────────────────────────────

def _deploy(aws, tmp_path, environment="staging"):
    from core.agents.ecs_agent import ECSAgent
    from core.schemas import ECSDeployRecord
    agent = ECSAgent()
    req = _req(edge="alb_waf", edge_cost_approved=True, https=True, database="rds", db_cost_approved=True,
               environment=environment)
    agent._runtime[id(req)] = _runtime(_app(tmp_path), environment=environment)
    rec = ECSDeployRecord(project_id="p1", cluster=req.cluster, service=req.service, region=REGION)
    asyncio.run(agent._step_provision(req, rec, aws))
    asyncio.run(agent._step_database_ready(req, rec, aws))
    return agent, req, rec


def test_https_and_rds_are_provisioned_and_wired_into_the_task(aws, tmp_path):
    agent, req, rec = _deploy(aws, tmp_path)
    cdn = agent._cdn[id(req)]
    assert req.cloudfront_domain == cdn.domain and cdn.url.startswith("https://") and "https://" in rec.provisioned["https"]
    dist = aws["cloudfront"].get_distribution_config(Id=cdn.distribution_id)["DistributionConfig"]
    assert dist["Origins"]["Items"][0]["DomainName"] == agent._edge[id(req)].dns_name
    assert dist["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    assert dist["DefaultCacheBehavior"]["CachePolicyId"] == aws_cdn.CACHING_DISABLED
    # ALB 는 CloudFront 주소 목록에서만 받는다
    lb = aws["elbv2"].describe_load_balancers(LoadBalancerArns=[agent._edge[id(req)].alb_arn])["LoadBalancers"][0]
    sg = aws["ec2"].describe_security_groups(GroupIds=lb["SecurityGroups"])["SecurityGroups"][0]
    assert sg["GroupName"] == "recoder-shop-alb-cf-sg" and sg["IpPermissions"][0]["PrefixListIds"]
    # RDS — 인터넷에서 닿지 않고, 태스크 보안 그룹에서만
    db = aws["rds"].describe_db_instances(DBInstanceIdentifier=aws_rds.db_identifier("recoder-shop"))["DBInstances"][0]
    assert db["PubliclyAccessible"] is False and db["DeletionProtection"] is True and db["StorageEncrypted"] is True
    runtime = agent._runtime[id(req)]
    assert db["Endpoint"]["Address"] in runtime.env["DATABASE_URL"]
    secret = json.loads(aws["secretsmanager"].get_secret_value(SecretId=aws_rds.secret_name("recoder-shop"))["SecretString"])
    assert secret["DATABASE_URL"] == runtime.env["DATABASE_URL"]
    assert "준비됨" in rec.provisioned["database"]
    # 태스크 정의 — 초기화 컨테이너, 앱은 그 뒤, 방패는 CloudFront 모드
    from core import shield as core_shield
    d = core_shield.decide(enabled=True, level="normal", public_port=3000, port_from_env=True)
    td = agent._render_task_definition(req, image="img", execution_role_arn="arn:aws:iam::123456789012:role/x",
                                       task_role_arn="", shield_decision=d, runtime=runtime)
    names = [c["name"] for c in td["containerDefinitions"]]
    assert "recoder-db-init" in names and "recoder-postgres" not in names
    app = td["containerDefinitions"][0]
    assert {"containerName": "recoder-db-init", "condition": "COMPLETE"} in app["dependsOn"]
    env = {e["name"]: e["value"] for e in td["containerDefinitions"][-1]["environment"]}
    assert env["RECODER_SHIELD_PROXY"] == "cdn"


def test_production_uses_secret_references_not_plaintext(aws, tmp_path):
    agent, req, rec = _deploy(aws, tmp_path, environment="production")
    runtime = agent._runtime[id(req)]
    assert "DATABASE_URL" not in runtime.env
    assert req.secret_refs["DATABASE_URL"].endswith(":DATABASE_URL::")
    init = next(c for c in runtime.sidecars if c["name"] == "recoder-db-init")
    assert init["secrets"][0]["valueFrom"].endswith(":DATABASE_URL::")
    assert not any(e["name"] == "DATABASE_URL" for e in init["environment"])
    assert "GetSecretValue" in rec.provisioned["database_secret"]


def test_redeploy_reuses_db_and_password(aws, tmp_path):
    agent, req, rec = _deploy(aws, tmp_path)
    first = agent._runtime[id(req)].env["DATABASE_URL"]
    agent2, req2, rec2 = _deploy(aws, tmp_path)
    assert agent2._runtime[id(req2)].env["DATABASE_URL"] == first
    assert "다시 사용" in rec2.provisioned["database"] or "준비됨" in rec2.provisioned["database"]
    assert agent2._db[id(req2)].created is False


def test_custom_domain_puts_validation_record_and_waits_for_issue(aws):
    zone = aws["route53"].create_hosted_zone(Name="example.com", CallerReference="t")["HostedZone"]["Id"].rsplit("/", 1)[-1]
    state = aws_cdn.ensure_certificate(aws["acm_global"], aws["route53"], "shop.example.com",
                                       tags={"recoder:managed": "true"}, sleep=NOSLEEP)
    assert not state.issued and state.zone_id == zone and state.validation
    records = aws["route53"].list_resource_record_sets(HostedZoneId=zone)["ResourceRecordSets"]
    assert any(r["Type"] == "CNAME" and r["Name"].startswith("_") for r in records)
    again = aws_cdn.ensure_certificate(aws["acm_global"], aws["route53"], "shop.example.com", sleep=NOSLEEP)
    assert again.arn == state.arn                       # 같은 인증서를 다시 요청하지 않는다
    # 발급된 뒤(다음 배포): 배포에 도메인 + 별칭 레코드
    cdn = aws_cdn.ensure_distribution(aws["cloudfront"], name="recoder-shop-1a2b", origin="lb.example.com")
    after = aws_cdn.ensure_distribution(aws["cloudfront"], name="recoder-shop-1a2b", origin="lb.example.com",
                                        alias="shop.example.com", certificate_arn=state.arn)
    assert after.alias == "shop.example.com" and after.url == "https://shop.example.com" and not after.created
    aws_cdn.point_domain(aws["route53"], zone, "shop.example.com", cdn.domain)
    records = aws["route53"].list_resource_record_sets(HostedZoneId=zone)["ResourceRecordSets"]
    assert {r["Type"] for r in records if r["Name"].rstrip(".") == "shop.example.com"} == {"A", "AAAA"}


def test_cleanup_handles_cloudfront_in_two_runs_and_rds_with_final_snapshot(aws, tmp_path):
    _deploy(aws, tmp_path)
    plan = aws_cleanup.plan(aws, region=REGION)
    ids = {i["id"] for g in plan["groups"] for i in g["items"]}
    kinds = {i.split(":")[0] for i in ids}
    assert {"cloudfront", "rds_instance", "rds_subnet_group", "rds_parameter_group", "secret"} <= kinds
    out = aws_cleanup.delete(aws, sorted(ids), region=REGION, approved=True, sleep=NOSLEEP)
    by = {r["id"].split(":")[0]: r for r in out["results"]}
    assert by["cloudfront"].get("skipped") and "15분" in by["cloudfront"]["message"]
    assert by["rds_instance"]["ok"] and "최종 스냅숏" in by["rds_instance"]["message"]
    assert by["rds_subnet_group"]["ok"] and by["secret"]["ok"]
    assert aws["rds"].describe_db_snapshots()["DBSnapshots"]
    second = aws_cleanup.plan(aws, region=REGION)
    left = {i["id"] for g in second["groups"] for i in g["items"]}
    assert {i.split(":")[0] for i in left} == {"cloudfront"}
    out = aws_cleanup.delete(aws, sorted(left), region=REGION, approved=True, sleep=NOSLEEP)
    assert out["deleted"] == 1
    assert aws_cleanup.plan(aws, region=REGION)["groups"] == []


def test_extension_request_passes_https_and_database_options():
    from api.routes.deploy_ecs import ExtensionEcsDeployRequest
    for name in ("https", "custom_domain", "database", "db_cost_approved"):
        assert name in ExtensionEcsDeployRequest.model_fields


def test_dynamic_cleanup_clients_are_bundled_in_the_installer():
    """정리 기능은 클라이언트 이름을 목록으로 만들어 정적 검사(test_bundled_aws_services)에 안 잡힌다."""
    from release_check import BUNDLED_AWS_SERVICES, RUNTIME_IMPORTS
    names = {aws_cleanup.GLOBAL_CLIENTS.get(n, n) for n in aws_cleanup.CLIENTS}
    assert names <= set(BUNDLED_AWS_SERVICES), sorted(names - set(BUNDLED_AWS_SERVICES))
    assert {"aws_cdn", "aws_rds", "aws_edge", "aws_cleanup"} <= set(RUNTIME_IMPORTS)
