"""2.0.31 — 2.0.26~2.0.30 검토에서 나온 결함 수정.

  · CloudFront 뒤 WAF 는 CloudFront 에(전역 범위) — ALB 에 붙이면 모든 요청이 CloudFront 엣지 IP 로 묶인다
  · 원본 잠금: CloudFront 비밀 머리글이 맞는 요청만 ALB 가 앱으로(남의 CloudFront 배포 차단)
  · 정리 뒤 다시 배포: 삭제 예정(7일) 비밀을 되살린다
  · 2.0.26 이전 태스크 정의도 방패 대응 뒤 앞단(ALB·CloudFront)을 잊지 않는다
  · 정리: 인증서 사용 중·DB 작업 중은 '건너뜀', DB 가 남으면 딸린 것도 남김, S3 부분 실패 보고, 뒤에서 도는 작업
  · ALB 서브넷은 영역마다 공개 서브넷, Route 53 은 다른 곳을 가리키는 레코드를 덮어쓰지 않음
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time

import pytest

import aws_cleanup
from core import aws_cdn, aws_edge, aws_rds, shield
from core.agents import ecs_runtime

NOSLEEP = lambda _s: None  # noqa: E731
REGION = "us-east-1"


def _client_error(code: str, op: str = "Op"):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


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


def _app(tmp_path):
    files = {
        "package.json": json.dumps({"name": "shop", "type": "module", "dependencies": {"express": "^4.19.2", "pg": "^8.11.0"}}),
        "src/server.js": "import pg from 'pg';\nconst pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def _req(**kw):
    from core.schemas import ECSDeployRequest
    base = dict(project_id="p1", cluster="recoder-cluster", service="recoder-shop", region=REGION,
                container_port=3000, health_check_path="/health")
    base.update(kw)
    return ECSDeployRequest(**base)


def _provision(aws, tmp_path, **kw):
    from core.agents.ecs_agent import ECSAgent
    from core.schemas import ECSDeployRecord
    agent = ECSAgent()
    req = _req(**kw)
    if kw.get("database") == "rds":
        agent._runtime[id(req)] = ecs_runtime.plan(str(_app(tmp_path)), key="us-east-1-c-s", environment="staging",
                                                   provided=set(), container_port=3000, managed_db=True)
    rec = ECSDeployRecord(project_id="p1", cluster=req.cluster, service=req.service, region=REGION)
    asyncio.run(agent._step_provision(req, rec, aws))
    if kw.get("database") == "rds":
        asyncio.run(agent._step_database_ready(req, rec, aws))
    return agent, req, rec


def _default_action(aws, edge) -> str:
    return aws["elbv2"].describe_listeners(ListenerArns=[edge.listener_arn])["Listeners"][0]["DefaultActions"][0]["Type"]


# ── WAF 는 CloudFront 에 ─────────────────────────────────────────────────

def test_https_with_waf_puts_the_web_acl_on_cloudfront_and_moves_it_off_the_alb(aws, tmp_path):
    # 먼저 ALB + WAF(지역)로 배포 → 그 뒤 HTTPS 를 켠다(옛 배포에서 옮겨 오는 경로)
    agent, req, _ = _provision(aws, tmp_path, edge="alb_waf", edge_cost_approved=True)
    edge = agent._edge[id(req)]
    assert ":regional/webacl/" in edge.web_acl_arn
    agent, req, rec = _provision(aws, tmp_path, edge="alb_waf", edge_cost_approved=True, https=True)
    edge, cdn = agent._edge[id(req)], agent._cdn[id(req)]
    assert ":global/webacl/" in edge.web_acl_arn
    dist = aws["cloudfront"].get_distribution_config(Id=cdn.distribution_id)["DistributionConfig"]
    assert dist["WebACLId"] == edge.web_acl_arn
    # ALB 에서는 지역 웹 ACL 을 떼고 지웠다 — IP 당 제한이 CloudFront 엣지 주소로 묶이지 않게
    assert aws_edge.find_web_acl(aws["wafv2"], edge.name) is None
    assert any("CloudFront" in v for k, v in rec.provisioned.items() if k.startswith("domain_note_"))
    assert "CloudFront" in rec.provisioned["waf"]
    acl = aws["wafv2_global"].get_web_acl(Name=aws_edge.global_acl_name(edge.name, REGION), Scope="CLOUDFRONT",
                                          Id=aws_edge.find_web_acl(aws["wafv2_global"], aws_edge.global_acl_name(edge.name, REGION), "CLOUDFRONT")["Id"])
    assert aws_edge.rate_of(acl["WebACL"]["Rules"]) == (req.waf_rate_limit, req.waf_window)
    # WAF 를 끄면 CloudFront 에서 떼고 웹 ACL 도 지운다
    agent, req, rec = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True, https=True)
    dist = aws["cloudfront"].get_distribution_config(Id=cdn.distribution_id)["DistributionConfig"]
    assert dist["WebACLId"] == ""
    assert aws_edge.find_web_acl(aws["wafv2_global"], aws_edge.global_acl_name(edge.name, REGION), "CLOUDFRONT") is None


def test_global_waf_permission_error_is_a_readable_infra_error(aws, tmp_path, monkeypatch):
    from core.aws_infra import InfraError

    def limit(*_a, **_k):
        raise _client_error("WAFLimitsExceededException", "CreateWebACL")
    monkeypatch.setattr(aws["wafv2_global"], "create_web_acl", limit)
    with pytest.raises(InfraError) as err:
        _provision(aws, tmp_path, edge="alb_waf", edge_cost_approved=True, https=True)
    assert "2.0.31" in err.value.remedy


def test_old_policy_keeps_the_waf_on_the_alb_and_deploys(aws, tmp_path, monkeypatch):
    """권한표를 아직 안 바꾼 사용자 — 재배포가 멈추지 않고 2.0.30 처럼 ALB 에 WAF 를 둔다(안내)."""
    monkeypatch.setattr(aws["wafv2_global"], "list_web_acls", lambda **_k: (_ for _ in ()).throw(
        _client_error("AccessDeniedException", "ListWebACLs")))
    agent, req, rec = _provision(aws, tmp_path, edge="alb_waf", edge_cost_approved=True, https=True)
    edge, cdn = agent._edge[id(req)], agent._cdn[id(req)]
    assert ":regional/webacl/" in edge.web_acl_arn
    attached = aws["wafv2"].get_web_acl_for_resource(ResourceArn=edge.alb_arn)["WebACL"]["ARN"]
    assert attached == edge.web_acl_arn
    assert not aws["cloudfront"].get_distribution_config(Id=cdn.distribution_id)["DistributionConfig"].get("WebACLId")
    assert "권한표 업데이트" in rec.provisioned["waf"]
    assert any("2.0.31 템플릿" in v for k, v in rec.provisioned.items() if k.startswith("domain_note_"))


def test_regional_waf_stays_until_the_cloudfront_waf_has_spread(aws, tmp_path, monkeypatch):
    agent, req, _ = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True, https=True)
    edge = agent._edge[id(req)]
    # 예전(2.0.30) 처럼 ALB 에 지역 웹 ACL 이 붙어 있는 상태
    acl, _ = aws_edge.ensure_web_acl(aws["wafv2"], edge.name, rate_limit=2000, window=300, tags=None)
    aws_edge.associate(aws["wafv2"], acl, edge.alb_arn, sleep=NOSLEEP)
    real_find = aws_cdn._find
    monkeypatch.setattr(aws_cdn, "_find", lambda cf, c, **kw: {**real_find(cf, c, **kw), "Status": "InProgress"})
    agent, req, rec = _provision(aws, tmp_path, edge="alb_waf", edge_cost_approved=True, https=True)
    assert aws_edge.find_web_acl(aws["wafv2"], edge.name) is not None          # 아직 뗄 때가 아님
    assert any("다음 배포에서 뗍니다" in v for k, v in rec.provisioned.items() if k.startswith("domain_note_"))
    monkeypatch.setattr(aws_cdn, "_find", real_find)
    _provision(aws, tmp_path, edge="alb_waf", edge_cost_approved=True, https=True)
    assert aws_edge.find_web_acl(aws["wafv2"], edge.name) is None


def test_waf_off_does_not_strip_a_web_acl_the_user_attached(aws):
    cdn = aws_cdn.ensure_distribution(aws["cloudfront"], name="recoder-shop-1a2b", origin="lb.example.com")
    cf = aws["cloudfront"]
    cur = cf.get_distribution_config(Id=cdn.distribution_id)
    cfg = cur["DistributionConfig"]
    cfg["WebACLId"] = "arn:aws:wafv2:us-east-1:123456789012:global/webacl/company-acl/abc"
    cf.update_distribution(Id=cdn.distribution_id, IfMatch=cur["ETag"], DistributionConfig=cfg)
    aws_cdn.ensure_distribution(cf, name="recoder-shop-1a2b", origin="lb.example.com", web_acl_id="")
    assert cf.get_distribution_config(Id=cdn.distribution_id)["DistributionConfig"]["WebACLId"].endswith("company-acl/abc")


# ── 원본 잠금 ────────────────────────────────────────────────────────────

def test_new_distribution_locks_the_alb_to_the_cloudfront_secret_header(aws, tmp_path):
    agent, req, rec = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True, https=True)
    edge, cdn = agent._edge[id(req)], agent._cdn[id(req)]
    dist = aws["cloudfront"].get_distribution_config(Id=cdn.distribution_id)["DistributionConfig"]
    header = dist["Origins"]["Items"][0]["CustomHeaders"]["Items"][0]
    assert header["HeaderName"] == aws_edge.ORIGIN_HEADER and len(header["HeaderValue"]) >= 40
    assert _default_action(aws, edge) == "fixed-response"
    rule = aws_edge.origin_rule(aws["elbv2"], edge.listener_arn)
    assert rule["Conditions"][0]["HttpHeaderConfig"]["Values"] == [header["HeaderValue"]]
    assert rule["Actions"][0]["TargetGroupArn"] == edge.target_group_arn
    assert "켜짐" in rec.provisioned["origin_lock"]
    # 다시 배포해도 같은 값 · 잠금 유지
    agent, req, rec = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True, https=True)
    assert agent._cdn[id(req)].origin_secret == header["HeaderValue"]
    assert _default_action(aws, agent._edge[id(req)]) == "fixed-response"
    # HTTPS 를 끄면 잠금을 푼다(ALB 를 직접 쓰므로)
    agent, req, rec = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True)
    assert _default_action(aws, agent._edge[id(req)]) == "forward"
    assert aws_edge.origin_rule(aws["elbv2"], edge.listener_arn) is None
    assert any("HTTPS 주소를 꺼" in v for k, v in rec.provisioned.items() if k.startswith("edge_note_"))


def test_existing_distribution_without_header_is_locked_only_on_the_next_deploy(aws, tmp_path):
    agent, req, _ = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True, https=True)
    edge, cdn = agent._edge[id(req)], agent._cdn[id(req)]
    # 2.0.27 로 만든 배포처럼 — 머리글 없음, 잠금 없음
    cf = aws["cloudfront"]
    current = cf.get_distribution_config(Id=cdn.distribution_id)
    cfg = current["DistributionConfig"]
    cfg["Origins"]["Items"][0]["CustomHeaders"] = {"Quantity": 0}
    cf.update_distribution(Id=cdn.distribution_id, IfMatch=current["ETag"], DistributionConfig=cfg)
    aws_edge.unlock_origin(aws["elbv2"], edge.listener_arn, edge.target_group_arn)
    agent, req, rec = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True, https=True)
    assert _default_action(aws, edge) == "forward"            # 머리글이 퍼지기 전엔 잠그지 않는다(403 방지)
    assert "준비 중" in rec.provisioned["origin_lock"]
    assert aws_cdn._origin_header(cf.get_distribution_config(Id=cdn.distribution_id)["DistributionConfig"])
    agent, req, rec = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True, https=True)
    assert _default_action(aws, edge) == "fixed-response" and "켜짐" in rec.provisioned["origin_lock"]


def test_lock_failure_with_old_policy_does_not_fail_the_deploy(aws, tmp_path, monkeypatch):
    monkeypatch.setattr(aws["elbv2"], "create_rule", lambda **_k: (_ for _ in ()).throw(
        _client_error("AccessDenied", "CreateRule")))
    agent, req, rec = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True, https=True)
    assert "켜지 못함" in rec.provisioned["origin_lock"] and "권한 템플릿" in rec.provisioned["origin_lock"]
    assert _default_action(aws, agent._edge[id(req)]) == "forward"


def test_plain_alb_redeploy_does_not_need_describe_rules(aws, tmp_path, monkeypatch):
    """옛 권한표(DescribeRules 없음)로 하는 평소 ALB 재배포는 그대로 돈다."""
    _provision(aws, tmp_path, edge="alb", edge_cost_approved=True)
    monkeypatch.setattr(aws["elbv2"], "describe_rules", lambda **_k: (_ for _ in ()).throw(
        _client_error("AccessDenied", "DescribeRules")))
    _provision(aws, tmp_path, edge="alb", edge_cost_approved=True)


# ── 정리 뒤 다시 배포 — 삭제 예정 비밀 ───────────────────────────────────

def test_redeploy_after_cleanup_restores_the_scheduled_secret(aws):
    sm = aws["secretsmanager"]
    name = aws_rds.secret_name("recoder-shop")
    arn, password = aws_rds._secret(sm, name, {"recoder:managed": "true"}, create=True)
    sm.delete_secret(SecretId=arn, RecoveryWindowInDays=7)
    with pytest.raises(Exception):
        sm.get_secret_value(SecretId=name)
    again, same = aws_rds._secret(sm, name, None, create=True)
    assert again == arn and same == password
    assert not sm.describe_secret(SecretId=name).get("DeletedDate")


def test_restore_failure_is_explained(aws, monkeypatch):
    from core.aws_infra import InfraError
    sm = aws["secretsmanager"]
    name = aws_rds.secret_name("recoder-shop")
    arn, _ = aws_rds._secret(sm, name, None, create=True)
    sm.delete_secret(SecretId=arn, RecoveryWindowInDays=7)
    monkeypatch.setattr(sm, "restore_secret", lambda **_k: (_ for _ in ()).throw(_client_error("AccessDeniedException")))
    with pytest.raises(InfraError) as err:
        aws_rds._secret(sm, name, None, create=True)
    assert "RestoreSecret" in err.value.remedy


# ── 방패: 2.0.26 이전 태스크 정의 ────────────────────────────────────────

@pytest.mark.parametrize("trust_cdn", [False, True])
def test_reconfigure_keeps_the_front_for_task_definitions_without_proxy_env(trust_cdn):
    d = shield.decide(enabled=True, level="normal", public_port=3000, port_from_env=True)
    td = shield.apply({"containerDefinitions": [{"name": "app", "image": "x"}], "memory": "512"}, "app", d,
                      trust_vpc_proxy=True, trust_cdn=trust_cdn)
    guard = td["containerDefinitions"][-1]
    guard["environment"] = [e for e in guard["environment"] if e["name"] != "RECODER_SHIELD_PROXY"]
    assert shield.current_settings(td)["proxy"] == ("cdn" if trust_cdn else "vpc")
    new = shield.reconfigure(td, deny_add=["8.8.8.8"])
    env = {e["name"]: e["value"] for e in new["containerDefinitions"][-1]["environment"]}
    conf = base64.b64decode(env["RECODER_SHIELD_CONF_B64"]).decode()
    assert env["RECODER_SHIELD_PROXY"] == ("cdn" if trust_cdn else "vpc")
    assert ("$http_cloudfront_viewer_address" in conf) == trust_cdn
    assert "real_ip_header X-Forwarded-For" in conf
    plain = shield.apply({"containerDefinitions": [{"name": "app", "image": "x"}], "memory": "512"}, "app", d)
    assert shield.current_settings(plain)["proxy"] == ""


# ── ALB 서브넷 ───────────────────────────────────────────────────────────

def test_alb_uses_public_subnets_even_when_tasks_run_in_private_ones(aws):
    ec2 = aws["ec2"]
    vpc = ec2.create_vpc(CidrBlock="10.9.0.0/16")["Vpc"]["VpcId"]
    igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
    public_rt = ec2.create_route_table(VpcId=vpc)["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=public_rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)
    sub = {}
    for i, (zone, kind) in enumerate([("us-east-1a", "pub"), ("us-east-1a", "priv"), ("us-east-1b", "pub"),
                                      ("us-east-1b", "priv")]):
        sid = ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.9.{i}.0/24", AvailabilityZone=zone)["Subnet"]["SubnetId"]
        sub[(zone, kind)] = sid
        if kind == "pub":
            ec2.associate_route_table(RouteTableId=public_rt, SubnetId=sid)
    chosen = aws_edge.alb_subnets(ec2, vpc, [sub[("us-east-1a", "priv")], sub[("us-east-1b", "priv")]])
    assert sorted(chosen) == sorted([sub[("us-east-1a", "pub")], sub[("us-east-1b", "pub")]])


# ── WAF 재시도 ───────────────────────────────────────────────────────────

class _FlakyWaf:
    def __init__(self, fail_times: int, code: str):
        self.left, self.code, self.calls = fail_times, code, 0

    def associate_web_acl(self, **_k):
        self.calls += 1
        if self.left:
            self.left -= 1
            raise _client_error(self.code)


def test_associate_waits_up_to_three_minutes_for_a_new_alb():
    waf = _FlakyWaf(30, "WAFUnavailableEntityException")
    aws_edge.associate(waf, "acl", "alb", sleep=NOSLEEP)
    assert waf.calls == 31


def test_detach_retries_delete_and_never_fails_the_deploy(monkeypatch):
    class Waf:
        deleted = 0

        def get_web_acl_for_resource(self, **_k):
            return {"WebACL": {"Name": "recoder-shop-1a2b"}}

        def disassociate_web_acl(self, **_k):
            pass

        def list_web_acls(self, **_k):
            return {"WebACLs": [{"Name": "recoder-shop-1a2b", "Id": "i", "ARN": "a"}]}

        def get_web_acl(self, **_k):
            return {"LockToken": "t"}

        def delete_web_acl(self, **_k):
            self.deleted += 1
            if self.deleted < 3:
                raise _client_error("WAFAssociatedItemException")

    waf = Waf()
    assert "떼고 지웠습니다" in aws_edge.detach_recoder_acl(waf, "alb", "recoder-shop-1a2b", sleep=NOSLEEP)
    assert waf.deleted == 3
    stuck = Waf()
    stuck.deleted = -100
    note = aws_edge.detach_recoder_acl(stuck, "alb", "recoder-shop-1a2b", sleep=NOSLEEP)
    assert "지우지 못했습니다" in note and "AWS 리소스 정리" in note


# ── 도메인 ───────────────────────────────────────────────────────────────

def test_route53_record_pointing_elsewhere_is_not_overwritten(aws):
    r53 = aws["route53"]
    zone = r53.create_hosted_zone(Name="example.com", CallerReference="t")["HostedZone"]["Id"].rsplit("/", 1)[-1]
    r53.change_resource_record_sets(HostedZoneId=zone, ChangeBatch={"Changes": [{"Action": "UPSERT", "ResourceRecordSet": {
        "Name": "shop.example.com", "Type": "A", "TTL": 60, "ResourceRecords": [{"Value": "203.0.113.5"}]}}]})
    notes = aws_cdn.point_domain(r53, zone, "shop.example.com", "d111.cloudfront.net")
    assert len(notes) == 1 and "203.0.113.5" in notes[0]
    records = {r["Type"]: r for r in r53.list_resource_record_sets(HostedZoneId=zone)["ResourceRecordSets"]
               if r["Name"].rstrip(".") == "shop.example.com"}
    assert records["A"]["ResourceRecords"][0]["Value"] == "203.0.113.5"       # 그대로
    assert records["AAAA"]["AliasTarget"]["DNSName"].rstrip(".") == "d111.cloudfront.net"
    # 이미 CloudFront 별칭이면 새 배포 주소로 바꾼다(A 는 여전히 그대로)
    assert len(aws_cdn.point_domain(r53, zone, "shop.example.com", "d222.cloudfront.net")) == 1
    records = {r["Type"]: r for r in r53.list_resource_record_sets(HostedZoneId=zone)["ResourceRecordSets"]
               if r["Name"].rstrip(".") == "shop.example.com"}
    assert records["AAAA"]["AliasTarget"]["DNSName"].rstrip(".") == "d222.cloudfront.net"


def test_certificate_idempotency_token_differs_for_similar_domains():
    class Acm:
        tokens: list = []

        def list_certificates(self, **_k):
            return {"CertificateSummaryList": []}

        def request_certificate(self, **k):
            self.tokens.append(k["IdempotencyToken"])
            return {"CertificateArn": f"arn:aws:acm:us-east-1:1:certificate/{len(self.tokens)}"}

        def describe_certificate(self, **_k):
            return {"Certificate": {"Status": "PENDING_VALIDATION", "DomainValidationOptions": [
                {"ResourceRecord": {"Name": "_x.", "Type": "CNAME", "Value": "_y."}}]}}

    class R53:
        def list_hosted_zones_by_name(self, **_k):
            raise _client_error("AccessDenied")

    acm = Acm()
    for d in ("a-b.example.com", "ab.example.com"):
        aws_cdn.ensure_certificate(acm, R53(), d, sleep=NOSLEEP)
    assert len(set(acm.tokens)) == 2 and all(len(t) == 32 and t.isalnum() for t in acm.tokens)


def test_route53_failure_becomes_a_note_not_a_failed_deploy(aws, tmp_path, monkeypatch):
    state = aws_cdn.CertificateState(arn="arn:aws:acm:us-east-1:123456789012:certificate/x", status="ISSUED", zone_id="Z1")
    monkeypatch.setattr(aws_cdn, "ensure_certificate", lambda *a, **k: state)
    monkeypatch.setattr(aws["route53"], "list_resource_record_sets", lambda **_k: (_ for _ in ()).throw(
        _client_error("AccessDenied")))
    agent, req, rec = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True, https=True,
                                 custom_domain="shop.example.com")
    assert "직접 확인" in rec.provisioned["domain"]
    assert any("Route 53 에 별칭을 넣지 못했습니다" in v for k, v in rec.provisioned.items() if k.startswith("domain_note_"))


# ── 정리 ─────────────────────────────────────────────────────────────────

def _item(kind, name, service="recoder-shop", **extra):
    return aws_cleanup.Item(id=f"{kind}:{name}", kind=kind, name=name, arn=f"arn:{name}", service=service, tagged=True,
                            extra=extra)


def _delete_with(monkeypatch, items, clients):
    monkeypatch.setattr(aws_cleanup, "find", lambda *_a, **_k: (items, []))
    return aws_cleanup.delete(clients, [i.id for i in items], region=REGION, approved=True, sleep=NOSLEEP)


def test_certificate_in_use_is_kept_not_failed(monkeypatch):
    class Acm:
        def delete_certificate(self, **_k):
            raise _client_error("ResourceInUseException")
    out = _delete_with(monkeypatch, [_item("acm_certificate", "shop.example.com")], {"acm_global": Acm()})
    r = out["results"][0]
    assert r["skipped"] and not r["ok"] and "CloudFront" in r["message"] and out["failed"] == 0


def test_busy_db_is_kept_and_its_secret_and_groups_stay(monkeypatch):
    from core import aws_rds as core_rds

    def busy(_rds, _ident):
        raise core_rds.DbBusy("지금 'creating' 상태라 지울 수 없습니다")
    monkeypatch.setattr(core_rds, "delete_instance", busy)
    called = []

    class Anything:
        def __getattr__(self, name):
            return lambda **k: called.append(name)
    items = [_item("rds_instance", "recoder-shop-1a2b-db"), _item("rds_subnet_group", "recoder-shop-1a2b-db-subnets"),
             _item("rds_parameter_group", "recoder-shop-1a2b-db-params"), _item("secret", "recoder/recoder-shop/database"),
             _item("secret", "recoder/other/database", service="other")]
    out = _delete_with(monkeypatch, items, {"rds": Anything(), "secretsmanager": Anything()})
    by = {r["id"]: r for r in out["results"]}
    assert by["rds_instance:recoder-shop-1a2b-db"]["skipped"]
    for kept in ("rds_subnet_group:recoder-shop-1a2b-db-subnets", "rds_parameter_group:recoder-shop-1a2b-db-params",
                 "secret:recoder/recoder-shop/database"):
        assert by[kept]["skipped"] and "DB 가 아직 남아" in by[kept]["message"]
    assert by["secret:recoder/other/database"]["ok"]           # 다른 서비스의 것은 그대로 지운다
    assert called == ["delete_secret"]


def test_db_still_deleting_after_wait_keeps_dependents(monkeypatch):
    from core import aws_rds as core_rds
    monkeypatch.setattr(core_rds, "delete_instance", lambda *_a: "지우기 시작했습니다.")

    class Rds:
        def describe_db_instances(self, **_k):
            return {"DBInstances": [{"DBInstanceStatus": "deleting"}]}
    out = _delete_with(monkeypatch, [_item("rds_instance", "recoder-shop-1a2b-db"), _item("secret", "recoder/recoder-shop/database")],
                       {"rds": Rds(), "secretsmanager": object()})
    assert all(r["skipped"] for r in out["results"])
    assert "다음 정리 때" in out["results"][0]["message"]


def test_s3_partial_delete_is_reported(monkeypatch):
    class S3:
        def list_objects_v2(self, **_k):
            return {"Contents": [{"Key": "a"}], "IsTruncated": False}

        def delete_objects(self, **_k):
            return {"Errors": [{"Key": "a", "Code": "AccessDenied", "Message": "no"}]}

        def delete_bucket(self, **_k):
            raise AssertionError("비우지 못한 버킷을 지우려 하면 안 된다")
    out = _delete_with(monkeypatch, [_item("s3_bucket", "recoder-site")], {"s3": S3()})
    assert out["failed"] == 1 and "AccessDenied" in out["results"][0]["message"]


def test_cloudfront_waf_still_attached_is_kept(monkeypatch):
    class Waf:
        def list_web_acls(self, **_k):
            return {"WebACLs": [{"Name": "recoder-shop-1a2b", "Id": "i", "ARN": "a"}]}

        def get_web_acl(self, **_k):
            return {"LockToken": "t"}

        def delete_web_acl(self, **_k):
            raise _client_error("WAFAssociatedItemException")
    out = _delete_with(monkeypatch, [_item("waf_cdn", "recoder-shop-1a2b", id="i")], {"wafv2_global": Waf()})
    assert out["results"][0]["skipped"] and "15분" in out["results"][0]["message"]


def test_full_cleanup_of_https_waf_db_deploy(aws, tmp_path):
    _provision(aws, tmp_path, edge="alb_waf", edge_cost_approved=True, https=True, database="rds", db_cost_approved=True)
    plan = aws_cleanup.plan(aws, region=REGION)
    ids = sorted(i["id"] for g in plan["groups"] for i in g["items"])
    assert any(i.startswith("waf_cdn:") for i in ids) and plan["summary"]["web_acls"] >= 1
    first = aws_cleanup.delete(aws, ids, region=REGION, approved=True, sleep=NOSLEEP)
    assert first["failed"] == 0, first["results"]
    left = sorted(i["id"] for g in aws_cleanup.plan(aws, region=REGION)["groups"] for i in g["items"])
    second = aws_cleanup.delete(aws, left, region=REGION, approved=True, sleep=NOSLEEP)
    assert second["failed"] == 0
    assert aws_cleanup.plan(aws, region=REGION)["groups"] == []


# ── 뒤에서 도는 정리 작업 ────────────────────────────────────────────────

def test_background_cleanup_job_reports_progress_and_blocks_overlap(monkeypatch):
    from fastapi import HTTPException
    from api.routes import aws_cleanup as route
    gate = threading.Event()

    def slow_delete(clients, ids, *, region, approved, records=(), on_progress=None, **_k):
        assert approved
        on_progress({"done": 0, "total": len(ids), "current": "로드밸런서 recoder-shop"})
        gate.wait(5)
        return {"region": region, "results": [{"id": i, "ok": True, "message": "지웠습니다."} for i in ids],
                "gaps": [], "deleted": len(ids), "failed": 0}
    monkeypatch.setattr(route.aws_cleanup, "delete", slow_delete)
    job = route.start_job("ap-northeast-2", ["alb:recoder-shop-1a2b"], clients={}, records=[])
    assert job["status"] == "running"
    with pytest.raises(HTTPException) as busy:
        route.start_job("ap-northeast-2", ["alb:x"], clients={}, records=[])
    assert busy.value.status_code == 409
    for _ in range(50):
        if route.job_view(job["id"])["progress"].get("current"):
            break
        time.sleep(0.02)
    assert route.job_view(job["id"])["progress"]["current"] == "로드밸런서 recoder-shop"
    gate.set()
    for _ in range(100):
        view = route.job_view(job["id"])
        if view["status"] != "running":
            break
        time.sleep(0.02)
    assert view["status"] == "done" and view["result"]["deleted"] == 1
    # 끝났으면 다시 시작할 수 있다
    gate.set()
    again = route.start_job("ap-northeast-2", ["alb:x"], clients={}, records=[])
    for _ in range(100):
        if route.job_view(again["id"])["status"] != "running":
            break
        time.sleep(0.02)
    with pytest.raises(HTTPException) as missing:
        route.job_view("000000000000")
    assert missing.value.status_code == 404


def test_background_job_failure_is_reported(monkeypatch):
    from api.routes import aws_cleanup as route

    def boom(*_a, **_k):
        raise _client_error("ExpiredToken")
    monkeypatch.setattr(route.aws_cleanup, "delete", boom)
    job = route.start_job("eu-west-1", ["alb:x"], clients={}, records=[])
    for _ in range(100):
        view = route.job_view(job["id"])
        if view["status"] != "running":
            break
        time.sleep(0.02)
    assert view["status"] == "failed" and "AWS 에 연결하지 못했습니다" in view["error"]


def test_cleanup_clients_include_the_global_waf_client_and_are_bundled():
    from release_check import BUNDLED_AWS_SERVICES
    assert aws_cleanup.GLOBAL_CLIENTS == {"acm_global": "acm", "wafv2_global": "wafv2"}
    names = {aws_cleanup.GLOBAL_CLIENTS.get(n, n) for n in aws_cleanup.CLIENTS}
    assert names <= set(BUNDLED_AWS_SERVICES)


def test_policy_allows_the_cloudfront_scope_waf_and_origin_lock():
    from core import aws_policy as ap
    policy = ap.build_policy()
    by = {s["Sid"]: s for s in policy["Statement"]}
    assert any(":global/webacl/recoder-*/*" in r for r in by["EdgeWaf"]["Resource"])
    assert "us-east-1" in by["EdgeWafRead"]["Condition"]["StringEquals"]["aws:RequestedRegion"]
    assert {"elasticloadbalancing:CreateRule", "elasticloadbalancing:ModifyRule",
            "elasticloadbalancing:DeleteRule"} <= set(by["EdgeOriginLock"]["Action"])
    assert "secretsmanager:RestoreSecret" in by["ManagedDatabaseSecret"]["Action"]


def test_cloudfront_waf_listing_denied_is_quiet_without_https(monkeypatch):
    class Denied:
        def list_web_acls(self, **_k):
            raise _client_error("AccessDeniedException")

    class Empty:
        def get_paginator(self, _op):
            class P:
                def paginate(self, **_k):
                    return [{}]
            return P()

        def __getattr__(self, _name):
            return lambda **_k: {}
    clients = {name: Empty() for name in aws_cleanup.CLIENTS}
    clients["wafv2_global"] = Denied()
    items, gaps = aws_cleanup.find(clients, region=REGION)
    assert not any("HTTPS 주소 WAF" in g for g in gaps)


def test_header_not_live_while_distribution_is_in_progress(aws, monkeypatch):
    first = aws_cdn.ensure_distribution(aws["cloudfront"], name="recoder-shop-1a2b", origin="lb.example.com")
    real_find = aws_cdn._find
    monkeypatch.setattr(aws_cdn, "_find", lambda cf, c, **kw: {**real_find(cf, c, **kw), "Status": "InProgress"})
    again = aws_cdn.ensure_distribution(aws["cloudfront"], name="recoder-shop-1a2b", origin="lb.example.com")
    assert again.origin_secret == first.origin_secret and not again.origin_secret_live


def test_changed_header_widens_an_existing_lock_instead_of_locking_users_out(aws, tmp_path):
    agent, req, _ = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True, https=True)
    edge, cdn = agent._edge[id(req)], agent._cdn[id(req)]
    old = cdn.origin_secret
    cf = aws["cloudfront"]
    cur = cf.get_distribution_config(Id=cdn.distribution_id)
    cfg = cur["DistributionConfig"]
    cfg["Origins"]["Items"][0]["CustomHeaders"] = {"Quantity": 0}           # 누가 머리글을 지움
    cf.update_distribution(Id=cdn.distribution_id, IfMatch=cur["ETag"], DistributionConfig=cfg)
    agent, req, rec = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True, https=True)
    new = agent._cdn[id(req)].origin_secret
    assert new != old and "옛 값과 새 값을 함께" in rec.provisioned["origin_lock"]
    values = aws_edge._header_condition(aws_edge.origin_rule(aws["elbv2"], edge.listener_arn))["HttpHeaderConfig"]["Values"]
    assert set(values) == {old, new}
    _provision(aws, tmp_path, edge="alb", edge_cost_approved=True, https=True)
    values = aws_edge._header_condition(aws_edge.origin_rule(aws["elbv2"], edge.listener_arn))["HttpHeaderConfig"]["Values"]
    assert values == [new]


def test_lock_uses_a_free_priority_next_to_user_rules(aws, tmp_path):
    agent, req, _ = _provision(aws, tmp_path, edge="alb", edge_cost_approved=True)
    edge = agent._edge[id(req)]
    aws["elbv2"].create_rule(ListenerArn=edge.listener_arn, Priority=10, Actions=[{"Type": "forward", "TargetGroupArn": edge.target_group_arn}],
                             Conditions=[{"Field": "path-pattern", "PathPatternConfig": {"Values": ["/admin/*"]}}])
    aws_edge.lock_origin(aws["elbv2"], edge.listener_arn, edge.target_group_arn, "s3cret")
    rule = aws_edge.origin_rule(aws["elbv2"], edge.listener_arn)
    assert rule["Priority"] == "11"


def test_point_domain_without_list_permission_falls_back_to_upsert(aws, monkeypatch):
    r53 = aws["route53"]
    zone = r53.create_hosted_zone(Name="example.com", CallerReference="t2")["HostedZone"]["Id"].rsplit("/", 1)[-1]
    monkeypatch.setattr(r53, "list_resource_record_sets", lambda **_k: (_ for _ in ()).throw(_client_error("AccessDenied")))
    assert aws_cdn.point_domain(r53, zone, "shop.example.com", "d111.cloudfront.net") == []


def test_sync_delete_goes_through_the_job_store(monkeypatch):
    from fastapi import HTTPException
    from api.routes import aws_cleanup as route
    gate = threading.Event()

    def slow(clients, ids, *, region, approved, records=(), on_progress=None, **_k):
        gate.wait(5)
        return {"region": region, "results": [], "gaps": [], "deleted": len(ids), "failed": 0}
    monkeypatch.setattr(route.aws_cleanup, "delete", slow)
    monkeypatch.setattr(route, "clients_for", lambda _r: {})
    monkeypatch.setattr(route, "_records", lambda _r: [])
    body = route.DeleteRequest(region="sa-east-1", ids=["alb:recoder-x"], approved=True, count=1)

    async def run():
        task = asyncio.create_task(route.delete(body))
        await asyncio.sleep(0.1)
        with pytest.raises(HTTPException) as busy:
            route.start_job("sa-east-1", ["alb:y"], clients={}, records=[])
        assert busy.value.status_code == 409
        gate.set()
        return await task
    assert asyncio.run(run())["deleted"] == 1
