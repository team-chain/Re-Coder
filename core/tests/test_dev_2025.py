"""2.0.25 — 기본 방패(nginx 속도 제한) · 공격·장애 감지 알림."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ops_alerts
import shield


# ── 방패 결정·설정 ───────────────────────────────────────────────────────

def test_decide_needs_port_env_and_skips_static_sites():
    assert shield.decide(enabled=True, level="normal", public_port=3000, port_from_env=True).on
    d = shield.decide(enabled=True, level="normal", public_port=3000, port_from_env=False)
    assert not d.on and "PORT" in d.reason
    assert not shield.decide(enabled=True, level="normal", public_port=80, port_from_env=True, static_site=True).on
    assert not shield.decide(enabled=False, level="strict", public_port=3000, port_from_env=True).on
    assert shield.decide(enabled=True, level="weird", public_port=3000, port_from_env=True).level == "normal"
    on = shield.decide(enabled=True, level="strict", public_port=3000, port_from_env=True)
    assert (on.public_port, on.app_port, on.level) == (3000, 3001, "strict")


def test_nginx_conf_limits_requests_but_not_health_and_logs_json():
    conf = shield.nginx_conf(public_port=3000, app_port=3001, level="normal", health_path="/api/health")
    assert "rate=20r/s" in conf and "burst=40 nodelay" in conf and "limit_req_status 429" in conf
    health = conf.split("location = /api/health {")[1].split("}")[0]
    assert "limit_req" not in health and "proxy_pass http://127.0.0.1:3001" in health
    assert '\\"src\\":\\"recoder-shield\\"' in conf or '"src":"recoder-shield"' in conf.replace("\\", "")
    assert "listen 3000;" in conf and "server_tokens off;" in conf
    assert "set_real_ip_from" not in conf
    assert "set_real_ip_from 10.0.0.0/8" in shield.nginx_conf(public_port=3000, app_port=3001, trust_vpc_proxy=True)


def test_deny_list_only_accepts_public_addresses():
    conf = shield.nginx_conf(public_port=3000, app_port=3001, deny=["1.2.3.4", "10.0.0.5", "127.0.0.1", "x;y", "5.6.7.0/24"])
    assert "deny 1.2.3.4;" in conf and "deny 5.6.7.0/24;" in conf
    assert "10.0.0.5" not in conf and "127.0.0.1;" not in conf.replace("proxy_pass http://127.0.0.1", "") and "x;y" not in conf


def _task_def(port=3000, with_health=True):
    app = {"name": "app", "image": "img", "essential": True, "portMappings": [{"containerPort": port, "protocol": "tcp"}],
           "environment": [{"name": "PORT", "value": str(port)}, {"name": "NODE_ENV", "value": "production"}],
           "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "/ecs/t", "awslogs-region": "us-east-1",
                                                                     "awslogs-stream-prefix": "ecs"}}}
    if with_health:
        app["healthCheck"] = {"command": ["CMD", "node", "-e", f"require('http').get(\"http://127.0.0.1:{port}/health\")"]}
    mock = {"name": "recoder-payment-mock", "environment": [{"name": "MOCK_WEBHOOK_URL", "value": f"http://127.0.0.1:{port}/api/webhooks/stripe"}]}
    return {"family": "t", "memory": "512", "cpu": "256", "containerDefinitions": [app, mock]}


def test_apply_moves_app_behind_shield():
    d = shield.decide(enabled=True, level="normal", public_port=3000, port_from_env=True)
    td = shield.apply(_task_def(), "app", d, health_path="/health")
    app, mock, sh = td["containerDefinitions"]
    assert {e["name"]: e["value"] for e in app["environment"]}["PORT"] == "3001"
    assert app["portMappings"] == [{"containerPort": 3001, "protocol": "tcp"}]
    assert "127.0.0.1:3001/health" in app["healthCheck"]["command"][-1]
    assert mock["environment"][0]["value"] == "http://127.0.0.1:3001/api/webhooks/stripe"
    assert sh["name"] == "recoder-shield" and sh["portMappings"] == [{"containerPort": 3000, "protocol": "tcp"}]
    assert sh["dependsOn"] == [{"containerName": "app", "condition": "HEALTHY"}]
    assert sh["logConfiguration"]["options"]["awslogs-stream-prefix"] == "recoder-shield"
    conf = base64.b64decode({e["name"]: e["value"] for e in sh["environment"]}["RECODER_SHIELD_CONF_B64"]).decode()
    assert "listen 3000;" in conf and "127.0.0.1:3001" in conf
    assert shield.apply(td, "app", d) is td and len(td["containerDefinitions"]) == 3  # 두 번 넣지 않는다


def test_apply_without_app_health_starts_after_app_and_off_decision_is_noop():
    d = shield.decide(enabled=True, level="loose", public_port=8000, port_from_env=True)
    td = shield.apply(_task_def(8000, with_health=False), "app", d)
    assert td["containerDefinitions"][-1]["dependsOn"] == [{"containerName": "app", "condition": "START"}]
    off = shield.decide(enabled=False, level="normal", public_port=8000, port_from_env=True)
    plain = _task_def(8000)
    assert shield.apply(plain, "app", off) == _task_def(8000)


def test_reconfigure_adds_deny_and_level_keeping_ports():
    d = shield.decide(enabled=True, level="normal", public_port=3000, port_from_env=True)
    td = shield.apply(_task_def(), "app", d)
    new = shield.reconfigure(td, level="strict", deny_add=["1.2.3.4", "192.168.0.1"])
    cur = shield.current_settings(new)
    assert cur == {"on": True, "level": "strict", "deny": ["1.2.3.4"], "proxy": ""}
    sh = new["containerDefinitions"][-1]
    conf = base64.b64decode({e["name"]: e["value"] for e in sh["environment"]}["RECODER_SHIELD_CONF_B64"]).decode()
    assert "rate=5r/s" in conf and "deny 1.2.3.4;" in conf and "listen 3000;" in conf
    assert shield.current_settings(td)["level"] == "normal"  # 원본은 그대로


def test_ecs_render_puts_shield_in_task_definition(tmp_path):
    from agents.ecs_agent import ECSAgent
    from schemas import ECSDeployRequest
    req = ECSDeployRequest(project_id="p", cluster="c", service="svc", region="us-east-1", container_port=3000,
                           target_group_arn="", health_check_path="/health")
    d = shield.decide(enabled=True, level="normal", public_port=3000, port_from_env=True)
    td = ECSAgent()._render_task_definition(req, image="img", execution_role_arn="arn:aws:iam::123456789012:role/x",
                                            task_role_arn="", shield_decision=d)
    names = [c["name"] for c in td["containerDefinitions"]]
    assert names == ["app", "recoder-shield"]
    assert {e["name"]: e["value"] for e in td["containerDefinitions"][0]["environment"]}["PORT"] == "3001"


def test_plan_shield_reads_port_usage_from_source(tmp_path):
    from agents.ecs_agent import ECSAgent
    from schemas import ECSDeployRequest
    (tmp_path / "package.json").write_text('{"name":"a","main":"server.js","dependencies":{"express":"^4.21.2"}}', encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM node:22-alpine\nCOPY . .\nCMD [\"node\",\"server.js\"]\n", encoding="utf-8")
    (tmp_path / "server.js").write_text("const app=require('express')();app.listen(process.env.PORT||3000);\n", encoding="utf-8")
    req = ECSDeployRequest(project_id="p", cluster="c", service="s", region="us-east-1", container_port=3000,
                           workspace_path=str(tmp_path))
    assert ECSAgent._plan_shield(req).on
    (tmp_path / "server.js").write_text("const app=require('express')();app.listen(3000);\n", encoding="utf-8")
    off = ECSAgent._plan_shield(req)
    assert not off.on and "PORT" in off.reason
    req.shield = False
    assert not ECSAgent._plan_shield(req).on


def test_extension_request_passes_shield_and_alert_options():
    from api.routes.deploy_ecs import ExtensionEcsDeployRequest, to_core_request
    core = to_core_request(ExtensionEcsDeployRequest(shield=False, shield_level="strict", alerts=True, alert_email="me@example.com"))
    assert (core.shield, core.shield_level, core.alerts, core.alert_email) == (False, "strict", True, "me@example.com")
    default = to_core_request(ExtensionEcsDeployRequest())
    assert (default.shield, default.shield_level, default.alerts, default.alert_email) == (True, "normal", True, "")
    with pytest.raises(Exception):
        ExtensionEcsDeployRequest(alert_email="not-an-email")


# ── 감지 판정 ─────────────────────────────────────────────────────────────

def test_spike_uses_fixed_threshold_on_first_day_and_median_after():
    assert ops_alerts.evaluate({"Requests": [10.0] * 20 + [3500.0]})[0]["kind"] == "spike"
    assert ops_alerts.evaluate({"Requests": [10.0] * 20 + [2900.0]}) == []
    day = [100.0] * 288
    assert ops_alerts.evaluate({"Requests": day + [999.0]}, history_points=288) == []
    hit = ops_alerts.evaluate({"Requests": day + [1000.0]}, history_points=288)
    assert hit[0]["kind"] == "spike" and hit[0]["baseline"] == 100.0
    quiet = [1.0] * 288   # 아주 조용한 서비스는 작은 흔들림(10배라도 300 미만)을 무시
    assert ops_alerts.evaluate({"Requests": quiet + [20.0]}, history_points=288) == []


def test_block_and_error_ratios_need_enough_requests():
    alerts = ops_alerts.evaluate({"Requests": [0, 200.0], "Blocked": [0, 60.0], "Errors": [0, 12.0]})
    assert {a["kind"] for a in alerts} == {"blocked", "errors"}
    assert ops_alerts.evaluate({"Requests": [0, 40.0], "Blocked": [0, 40.0], "Errors": [0, 40.0]}) == []


def test_outage_after_three_short_checks_only_once():
    w = ops_alerts.TaskWatch()
    assert w.observe("k", 0, 1) is None and w.observe("k", 0, 1) is None
    assert w.observe("k", 0, 1)["kind"] == "outage"
    assert w.observe("k", 0, 1) is None
    assert w.observe("k", 1, 1) is None and w.observe("k", 0, 0) is None


def test_suggestions_only_offer_what_applies():
    s = ops_alerts.suggestions({"kind": "spike"}, {"ips": [{"value": "8.8.4.4", "count": 900}]}, True)
    assert [x["action"] for x in s] == ["block_ip", "shield_strict", "scale"]
    assert [x["action"] for x in ops_alerts.suggestions({"kind": "spike"}, {"ips": [{"value": "10.0.0.2", "count": 9}]}, False)] == ["scale"]
    assert [x["action"] for x in ops_alerts.suggestions({"kind": "outage"}, {}, True)] == ["scale", "rollback"]


# ── AWS 쪽 준비·감시·대응 (moto) ─────────────────────────────────────────

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
        yield boto3


def test_setup_aws_creates_filters_topic_email_and_alarms(aws):
    s = aws.session.Session(region_name="us-east-1")
    logs = s.client("logs")
    logs.create_log_group(logGroupName="/ecs/shop")
    clients = {"logs": logs, "cloudwatch": s.client("cloudwatch"), "sns": s.client("sns")}
    out = ops_alerts.setup_aws(clients, service="shop-svc", log_group="/ecs/shop", email="me@example.com",
                               tags={"recoder:managed": "true"})
    assert out["filters"] == ["Requests", "Blocked", "Errors"] and out["email"] == "pending"
    filters = {f["filterName"]: f["filterPattern"] for f in logs.describe_metric_filters(logGroupName="/ecs/shop")["metricFilters"]}
    assert filters["recoder-shop-svc-blocked"] == '{ ($.src = "recoder-shield") && ($.status = 429) }'
    alarms = {a["AlarmName"] for a in clients["cloudwatch"].describe_alarms()["MetricAlarms"]}
    assert "recoder-shop-svc-traffic-spike" in alarms
    assert out["topic"].endswith(":recoder-shop-svc-alerts")


def test_setup_aws_without_permission_only_adds_notes():
    class Denied:
        def __getattr__(self, name):
            def fail(**_):
                raise RuntimeError("AccessDenied")
            return fail
    out = ops_alerts.setup_aws({"logs": Denied(), "cloudwatch": Denied(), "sns": Denied()}, service="s", log_group="/ecs/s")
    assert out["filters"] == [] and out["alarms"] == [] and len(out["notes"]) == 2


def test_monitor_raises_spike_and_outage_and_dedups(aws, monkeypatch):
    s = aws.session.Session(region_name="us-east-1")
    ecs = s.client("ecs")
    ecs.create_cluster(clusterName="c")
    td = ecs.register_task_definition(family="f", containerDefinitions=[{"name": "app", "image": "x", "memory": 128}])
    ecs.create_service(cluster="c", serviceName="svc", taskDefinition="f", desiredCount=1)
    s.client("logs").create_log_group(logGroupName="/ecs/f")
    cw = s.client("cloudwatch")
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 5)
    cw.put_metric_data(Namespace=ops_alerts.namespace("svc"), MetricData=[
        {"MetricName": "Requests", "Timestamp": now - timedelta(minutes=5), "Value": 5000.0, "Unit": "Count"},
        {"MetricName": "Blocked", "Timestamp": now - timedelta(minutes=5), "Value": 2500.0, "Unit": "Count"}])
    ops_alerts.register(region="us-east-1", cluster="c", service="svc", log_group="/ecs/f")
    sent = []

    class Short:  # moto 는 실행 태스크 수를 원하는 수로 둔다 — 태스크가 계속 죽는 상황을 흉내 낸다
        def __init__(self, inner):
            self.inner = inner

        def client(self, name):
            c = self.inner.client(name)
            if name != "ecs":
                return c

            class E:
                def describe_services(self, **kw):
                    out = c.describe_services(**kw)
                    for svc in out["services"]:
                        svc["runningCount"] = 0
                    return out
            return E()
    mon = ops_alerts.Monitor(lambda region: Short(s), notify=sent.append)
    monkeypatch.setattr(ops_alerts, "top_sources", lambda *a, **k: {"ips": [{"value": "8.8.4.4", "count": 4000}], "paths": []})
    first = mon.tick(now=now)
    kinds = {a["kind"] for a in first}
    assert {"spike", "blocked"} <= kinds and sent
    spike = next(a for a in first if a["kind"] == "spike")
    assert spike["suggestions"][0] == {"action": "block_ip", "value": "8.8.4.4", "label": "8.8.4.4 차단",
                                       "detail": spike["suggestions"][0]["detail"]}
    second, third = mon.tick(now=now), mon.tick(now=now)
    assert second == []                                   # 같은 알림은 30분 동안 다시 안 냄
    assert [a["kind"] for a in third] == ["outage"]       # 태스크 부족 3번째 확인
    # moto 서비스는 실행 태스크가 0 → 세 번째 확인에서 장애
    kinds_all = {a["kind"] for a in ops_alerts.alerts()}
    assert "outage" in kinds_all
    assert ops_alerts.alerts(since=first[-1]["id"]) == [a for a in ops_alerts.alerts() if a["ts"] > first[-1]["ts"]]


def test_act_block_ip_registers_new_revision_with_deny_and_requires_approval(aws):
    s = aws.session.Session(region_name="us-east-1")
    ecs = s.client("ecs")
    ecs.create_cluster(clusterName="c")
    d = shield.decide(enabled=True, level="normal", public_port=3000, port_from_env=True)
    td = shield.apply(_task_def(), "app", d)
    td["containerDefinitions"] = [dict(c, image=c.get("image", "img"), memory=128) for c in td["containerDefinitions"]]
    arn = ecs.register_task_definition(family="t", containerDefinitions=td["containerDefinitions"])["taskDefinition"]["taskDefinitionArn"]
    ecs.create_service(cluster="c", serviceName="svc", taskDefinition=arn, desiredCount=1)
    ops_alerts.register(region="us-east-1", cluster="c", service="svc", log_group="/ecs/t")
    alert = ops_alerts._store({"key": "us-east-1/c/svc", "kind": "spike", "region": "us-east-1", "cluster": "c", "service": "svc",
                               "suggestions": ops_alerts.suggestions({"kind": "spike"}, {"ips": [{"value": "8.8.4.4", "count": 1}]}, True)})
    with pytest.raises(ValueError):
        ops_alerts.act(alert["id"], "rollback", None, approved=True, session_factory=lambda r: s)  # 이 알림의 제안이 아님
    assert ops_alerts.act(alert["id"], "block_ip", "8.8.4.4", approved=False, session_factory=lambda r: s)["applied"] is False
    out = ops_alerts.act(alert["id"], "block_ip", "8.8.4.4", approved=True, session_factory=lambda r: s)
    assert out["applied"]
    svc = ecs.describe_services(cluster="c", services=["svc"])["services"][0]
    new = ecs.describe_task_definition(taskDefinition=svc["taskDefinition"])["taskDefinition"]
    assert shield.current_settings(new)["deny"] == ["8.8.4.4"] and new["revision"] == 2
    assert next(a for a in ops_alerts.alerts() if a["id"] == alert["id"])["status"] == "applied"


def test_alert_routes_list_and_act(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import ops_alerts as routes
    monkeypatch.setenv("RECODER_HOME", str(tmp_path))
    app = FastAPI()
    app.include_router(routes.router)
    c = TestClient(app)
    stored = ops_alerts._store({"key": "r/c/s", "kind": "outage", "region": "r", "cluster": "c", "service": "s",
                                "suggestions": [{"action": "rollback", "value": "", "label": "롤백"}]})
    body = c.get("/api/ops/alerts").json()
    assert body["alerts"][0]["id"] == stored["id"] and body["monitors"] == []
    assert c.get(f"/api/ops/alerts?since={stored['id']}").json()["alerts"] == []
    r = c.post(f"/api/ops/alerts/{stored['id']}/act", json={"action": "scale", "value": 2, "approved": True})
    assert r.status_code == 400
    r = c.post(f"/api/ops/alerts/{stored['id']}/act", json={"action": "rollback", "approved": True})
    assert r.status_code == 200 and r.json()["applied"] is False
