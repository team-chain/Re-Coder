"""2.0.48 24시간 감시자 — 설치 · 감지 · 알림 · 승인 링크 · 롤백 · 삭제 · 권한표.

AWS 는 moto 로 흉내 낸다(실제 계정 · 키를 쓰지 않는다). Lambda 코드(watcher_lambda/handler.py)는 실제 Lambda 와
같은 진입점(handler)에 실제 이벤트 모양(EventBridge · 함수 URL)을 넣어 돌린다.
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import re
import time
import zipfile
from pathlib import Path

import pytest

import aws_calls
import aws_onboarding
import aws_policy
import watcher

CORE = Path(__file__).resolve().parent.parent
REGION = "us-east-1"


# ---------------------------------------------------------------------------
# 순수 부분
# ---------------------------------------------------------------------------

def test_names_fit_aws_limits_and_never_collide():
    long = "a" * 200
    n = watcher.names(REGION, "c", long)
    assert len(n["function"]) <= 64 and all(len(r) <= 64 for r in n["rules"].values())
    assert n["function"].startswith("recoder-") and n["table"] == n["function"]
    assert watcher.names(REGION, "c", "shop")["function"] != watcher.names("ap-northeast-2", "c", "shop")["function"]
    assert watcher.names(REGION, "c", "shop")["function"] != watcher.names(REGION, "c2", "shop")["function"]
    assert watcher.names(REGION, "c", "shop") == watcher.names(REGION, "c", "shop")
    assert n["alarms"][2] == f"recoder-{long[:120]}-error-ratio"  # ops_alerts 가 만드는 이름과 같다


def test_package_is_deterministic_and_runs():
    a, b = watcher.package(), watcher.package()
    assert a == b and watcher.code_sha256(a) == watcher.code_sha256(b)
    with zipfile.ZipFile(io.BytesIO(a)) as z:
        assert z.namelist() == ["handler.py"]
        src = z.read("handler.py").decode()
    compile(src, "handler.py", "exec")
    assert "def handler(" in src and "import boto3" in src


def _lambda_calls() -> set[tuple[str, str]]:
    """handler.py 의 AWS 호출은 모두 _client("서비스").작업(...) 모양이다 — 그 밖의 boto3 사용이 없다는 것까지 확인한다."""
    import ast
    tree = ast.parse((CORE / "watcher_lambda" / "handler.py").read_text(encoding="utf-8"))
    calls, direct = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            inner = node.func.value
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "_client":
                assert isinstance(inner.args[0], ast.Constant), "서비스 이름은 글자 그대로"
                calls.add((inner.args[0].value, node.func.attr))
            if isinstance(inner, ast.Name) and inner.id == "boto3":
                direct.append(node.func.attr)
    assert direct == ["client"], f"_client 밖의 boto3 사용: {direct}"
    return calls


def test_watcher_role_policy_matches_lambda_calls_exactly():
    """감시자 Lambda 의 AWS 호출 = 감시자 역할 권한(빠진 것도, 남는 것도 없이)."""
    calls = _lambda_calls()
    assert ("ecs", "update_service") in calls and ("dynamodb", "update_item") in calls  # 스캔이 살아 있다
    used = {f"{aws_calls.SERVICE_TO_IAM_PREFIX.get(svc, svc)}:{aws_calls._pascal(op)}" for svc, op in calls}
    granted = set(aws_policy.used_actions(aws_policy.watcher_role_policy("123456789012")))
    logs = {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"}  # Lambda 런타임이 쓴다(코드 호출 아님)
    assert used - granted == set(), f"감시자 역할에 빠진 권한: {used - granted}"
    assert granted - used - logs == set(), f"감시자 역할에 쓰지 않는 권한: {granted - used - logs}"


def test_watcher_role_rollback_needs_the_watch_tag():
    stmts = {s["Sid"]: s for s in aws_policy.watcher_role_policy("123456789012")["Statement"]}
    assert stmts["ApprovedRollbackOnly"]["Action"] == ["ecs:UpdateService"]
    assert stmts["ApprovedRollbackOnly"]["Condition"] == {"StringEquals": {"aws:ResourceTag/recoder:watch": "true"}}
    assert all("iam:" not in a for s in stmts.values() for a in s["Action"])  # 역할 · 권한을 바꾸는 권한 없음
    assert aws_policy.watcher_trust_policy()["Statement"][0]["Principal"] == {"Service": "lambda.amazonaws.com"}


def test_deploy_policy_can_install_but_not_create_roles():
    policy = aws_policy.build_policy(["ecs"], "123456789012", REGION)
    actions = set(aws_policy.used_actions(policy))
    assert {"lambda:CreateFunction", "events:PutRule", "dynamodb:CreateTable", "lambda:CreateFunctionUrlConfig"} <= actions
    assert not {"iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy"} & actions
    passes = [s for s in policy["Statement"] if s["Sid"] == "WatcherPassRole"][0]
    assert passes["Resource"].endswith(":role/recoder-watcher")
    assert passes["Condition"] == {"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}}


def test_onboarding_template_creates_watcher_role_with_latin1_description():
    t = aws_onboarding.build_quickcreate_template()
    role = t["Resources"]["RecoderWatcherRole"]["Properties"]
    assert role["RoleName"] == "recoder-watcher"
    assert re.fullmatch(r"[\t\n\r\x20-\x7e\xa1-\xff]*", role["Description"])  # IAM 역할 설명 허용 글자
    assert role["AssumeRolePolicyDocument"] == aws_policy.watcher_trust_policy()
    doc = json.dumps(role["Policies"][0]["PolicyDocument"])
    assert "${AWS::AccountId}" in doc and "<ACCOUNT_ID>" not in doc


def test_offers_skip_used_expired_and_stale():
    now = 1_000_000.0
    def ev(status="offered", expires=now + 100, frm="td:2", target="td:1"):
        return {"sk": "s", "title": "t", "at": "a", "actions": [{"action": "rollback", "status": status, "expires": expires,
                                                              "from_td": frm, "target": target}]}
    assert len(watcher.offers([ev()], "td:2", now=now)) == 1
    assert watcher.offers([ev(status="used")], "td:2", now=now) == []
    assert watcher.offers([ev(expires=now - 1)], "td:2", now=now) == []
    assert watcher.offers([ev(frm="td:9")], "td:2", now=now) == []  # 그 뒤로 버전이 바뀜
    assert len(watcher.offers([ev(), ev()], "td:2", now=now)) == 1  # 같은 대상은 하나만


# ---------------------------------------------------------------------------
# moto
# ---------------------------------------------------------------------------

@pytest.fixture
def aws(monkeypatch, tmp_path):
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws
    for k in ("AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("RECODER_HOME", str(tmp_path))
    with mock_aws():
        s = boto3.session.Session(region_name=REGION)
        s.client("iam").create_role(RoleName="recoder-watcher",
                                    AssumeRolePolicyDocument=json.dumps(aws_policy.watcher_trust_policy()))
        ecs = s.client("ecs")
        ecs.create_cluster(clusterName="c")
        for image in ("app:v1", "app:v2"):
            ecs.register_task_definition(family="shop", containerDefinitions=[{"name": "app", "image": image, "memory": 128}])
        ecs.create_service(cluster="c", serviceName="shop", taskDefinition="shop:2", desiredCount=1)
        yield s


def td(rev: int) -> str:
    return f"arn:aws:ecs:{REGION}:123456789012:task-definition/shop:{rev}"


def current_td(s) -> str:
    return s.client("ecs").describe_services(cluster="c", services=["shop"])["services"][0]["taskDefinition"]


def installed(s, **kw):
    return watcher.install(s, region=REGION, cluster="c", service="shop", approved=True, stable_td=td(1),
                           sleep=lambda _: None, **kw)


def test_install_requires_approval_and_valid_discord(aws):
    with pytest.raises(PermissionError):
        watcher.install(aws, region=REGION, cluster="c", service="shop")
    with pytest.raises(watcher.WatchError):
        installed(aws, discord="https://evil.example.com/api/webhooks/1/abc")
    assert watcher.status(aws, region=REGION, cluster="c", service="shop") == {"installed": False}


def test_plan_shows_resources_and_role_state(aws):
    p = watcher.plan(aws, region=REGION, cluster="c", service="shop")
    assert p["role"]["exists"] is True and p["role"]["arn"].endswith(":role/recoder-watcher")
    assert {r["kind"] for r in p["resources"]} == {"Lambda", "DynamoDB", "EventBridge", "CloudWatch Logs"}
    missing = watcher.plan(aws, region=REGION, cluster="c", service="shop", role="no-such-role")
    assert missing["role"]["exists"] is False and "원클릭 IAM 셋업" in missing["notes"][0]
    with pytest.raises(watcher.WatchError):
        watcher.plan(aws, region=REGION, cluster="c", service="ghost")


def test_install_creates_everything_and_is_idempotent(aws):
    out = installed(aws, discord="https://discord.com/api/webhooks/123456/abcdefghijklmnopqrstuvwxyz")
    assert out["installed"] and out["url"]
    n = watcher.names(REGION, "c", "shop")
    lam = aws.client("lambda")
    fn = lam.get_function(FunctionName=n["function"])
    assert fn["Configuration"]["Runtime"] == "python3.13" and fn["Configuration"]["Role"].endswith("/recoder-watcher")
    assert fn["Configuration"]["Environment"]["Variables"]["WATCH_SERVICE"] == "shop"
    assert fn["Tags"]["recoder:managed"] == "true"
    sids = {st["Sid"] for st in json.loads(lam.get_policy(FunctionName=n["function"])["Policy"])["Statement"]}
    assert {"recoder-url-public", "recoder-url-invoke", "recoder-alarm", "recoder-task", "recoder-svc", "recoder-tick"} <= sids
    rules = {r["Name"]: r for r in aws.client("events").list_rules(NamePrefix=n["base"])["Rules"]}
    assert rules[n["rules"]["tick"]]["ScheduleExpression"] == "rate(5 minutes)"
    pattern = json.loads(rules[n["rules"]["alarm"]]["EventPattern"])
    assert pattern["detail"]["alarmName"] == n["alarms"]  # 정확한 이름만(접두어면 비슷한 이름의 다른 서비스 경보도 받는다)
    svc_arn = aws.client("ecs").describe_services(cluster="c", services=["shop"])["services"][0]["serviceArn"]
    assert json.loads(rules[n["rules"]["svc"]]["EventPattern"])["resources"] == [svc_arn]
    tags = aws.client("ecs").list_tags_for_resource(resourceArn=svc_arn)["tags"]
    assert {"key": "recoder:watch", "value": "true"} in tags
    assert aws.client("logs").describe_log_groups(logGroupNamePrefix=n["log_group"])["logGroups"][0]["retentionInDays"] == 14
    st = watcher.status(aws, region=REGION, cluster="c", service="shop")
    assert st["installed"] and st["discord"] and st["approval_link"] and st["stable"] == "shop:1" and st["waiting_first_tick"]
    again = installed(aws)  # 다시 설치 — 디스코드 주소 · 기준 버전은 그대로
    assert again["installed"]
    assert watcher.status(aws, region=REGION, cluster="c", service="shop")["discord"] is True


def test_uninstall_removes_everything_and_twice_is_fine(aws):
    installed(aws)
    n = watcher.names(REGION, "c", "shop")
    with pytest.raises(PermissionError):
        watcher.uninstall(aws, region=REGION, cluster="c", service="shop", approved=False)
    out = watcher.uninstall(aws, region=REGION, cluster="c", service="shop", approved=True)
    assert out["ok"], out
    assert aws.client("events").list_rules(NamePrefix=n["base"])["Rules"] == []
    assert watcher.status(aws, region=REGION, cluster="c", service="shop") == {"installed": False}
    svc_arn = aws.client("ecs").describe_services(cluster="c", services=["shop"])["services"][0]["serviceArn"]
    assert all(t["key"] != "recoder:watch" for t in aws.client("ecs").list_tags_for_resource(resourceArn=svc_arn)["tags"])
    assert watcher.uninstall(aws, region=REGION, cluster="c", service="shop", approved=True)["ok"]


def test_set_versions_only_when_installed(aws):
    assert watcher.set_versions(aws, region=REGION, cluster="c", service="shop", current_td=td(2), stable_td=td(1)) is False
    installed(aws)
    assert watcher.set_versions(aws, region=REGION, cluster="c", service="shop", current_td=td(3), stable_td=td(2)) is True
    assert watcher.status(aws, region=REGION, cluster="c", service="shop")["stable"] == "shop:2"
    assert watcher.set_versions(aws, region=REGION, cluster="c", service="shop", current_td=td(2), stable_td=td(2)) is False


def test_settings_validate_and_save(aws):
    with pytest.raises(watcher.WatchError):
        watcher.settings(aws, region=REGION, cluster="c", service="shop", discord="https://discord.com/api/webhooks/1/x")
    installed(aws)
    with pytest.raises(watcher.WatchError):
        watcher.settings(aws, region=REGION, cluster="c", service="shop", discord="http://discord.com/api/webhooks/123456/abcdefghijklmnopqrstu")
    ok = watcher.settings(aws, region=REGION, cluster="c", service="shop",
                          discord="https://discord.com/api/webhooks/123456/abcdefghijklmnopqrstuvwxyz", email="me@example.com")
    assert "디스코드" in ok["message"] and "구독 확인" in ok["message"]
    with pytest.raises(watcher.WatchError):
        watcher.settings(aws, region=REGION, cluster="c", service="shop", email="not-an-email")
    watcher.settings(aws, region=REGION, cluster="c", service="shop", discord="")
    assert watcher.status(aws, region=REGION, cluster="c", service="shop")["discord"] is False


def test_vscode_rollback_checks_version_and_approval(aws):
    installed(aws)
    with pytest.raises(PermissionError):
        watcher.rollback(aws, region=REGION, cluster="c", service="shop", target_td=td(1), from_td=td(2), approved=False)
    with pytest.raises(watcher.WatchError):
        watcher.rollback(aws, region=REGION, cluster="c", service="shop", target_td=td(1), from_td=td(9), approved=True)
    assert current_td(aws).endswith("shop:2")
    out = watcher.rollback(aws, region=REGION, cluster="c", service="shop", target_td=td(1), from_td=td(2), approved=True)
    assert out["ok"] and current_td(aws).endswith("shop:1")
    ev = watcher.events(aws, region=REGION, cluster="c", service="shop")
    assert ev[0]["kind"] == "rollback" and ev[0]["source"] == "vscode"


# ---------------------------------------------------------------------------
# Lambda 코드 — 실제 진입점에 실제 이벤트 모양
# ---------------------------------------------------------------------------

@pytest.fixture
def lam(aws, monkeypatch):
    installed(aws, discord="https://discord.com/api/webhooks/123456/abcdefghijklmnopqrstuvwxyz")
    n = watcher.names(REGION, "c", "shop")
    env = aws.client("lambda").get_function(FunctionName=n["function"])["Configuration"]["Environment"]["Variables"]
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("watcher_handler_under_test", CORE / "watcher_lambda" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    posted: list[dict] = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b""

    def urlopen(req, timeout=0):
        assert req.full_url.startswith("https://discord.com/api/webhooks/") and timeout <= 5
        posted.append(json.loads(req.data))
        return Resp()
    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    mod.posted = posted
    mod.names = n
    clock = {"t": time.time()}

    def tick():
        """5분 일정 한 번 — 시계를 5분 앞으로(같은 일정이 4분 안에 두 번 오면 한 번으로 센다)."""
        clock["t"] += 300
        return mod.handler({"source": "aws.events", "detail-type": "Scheduled Event"})
    def now():
        clock["t"] += 0.001  # 부를 때마다 조금씩 — 같은 시각에 생긴 기록도 순서가 정해지게
        return clock["t"]
    monkeypatch.setattr(mod, "_now", now)
    mod.tick_once = tick
    mod.clock = clock
    return mod


def _short(aws, monkeypatch, mod):
    """moto 는 실행 태스크 수를 원하는 수로 둔다 — 태스크가 계속 죽는 상황을 흉내 낸다."""
    real = mod._service

    def fake():
        svc = real()
        return {**svc, "runningCount": 0, "desiredCount": 1}
    monkeypatch.setattr(mod, "_service", fake)


def _http(method: str, path: str, *, token: str = "", body: str = "", b64: bool = False) -> dict:
    ev = {"version": "2.0", "rawPath": path, "requestContext": {"http": {"method": method, "path": path, "sourceIp": "203.0.113.9"}}}
    if token and method == "GET":
        ev["queryStringParameters"] = {"t": token}
    if body:
        ev["body"] = base64.b64encode(body.encode()).decode() if b64 else body
        ev["isBase64Encoded"] = b64
    return ev


def _link_token(mod) -> str:
    text = mod.posted[-1]["content"]
    m = re.search(r"/r\?t=([A-Za-z0-9_\-]+)", text)
    assert m, text
    return m.group(1)


def test_tick_heartbeat_then_outage_after_three_strikes_with_rollback_link(aws, lam, monkeypatch):
    assert lam.tick_once()["raised"] is False
    st = watcher.status(aws, region=REGION, cluster="c", service="shop")
    assert st["healthy"] and st["ticks"] == 1 and not st["waiting_first_tick"]
    _short(aws, monkeypatch, lam)
    results = [lam.tick_once() for _ in range(3)]
    assert [r["raised"] for r in results] == [False, True, False]  # 세 번째 실패에서 한 번, 그다음은 30분 쉼
    ev = watcher.events(aws, region=REGION, cluster="c", service="shop")
    assert ev[0]["kind"] == "outage" and ev[0]["actions"][0]["status"] == "offered"
    assert "token_id" not in ev[0]["actions"][0]  # 토큰 해시도 화면으로 보내지 않는다
    assert len(lam.posted) == 1 and "v1" not in lam.posted[0]["content"]
    assert lam.posted[0]["allowed_mentions"] == {"parse": []}
    token = _link_token(lam)
    items = aws.client("dynamodb").scan(TableName=lam.names["table"])["Items"]
    assert token not in json.dumps(items)  # 표에는 해시만
    offers = watcher.offers(ev, current_td(aws))
    assert offers and offers[0]["target"].endswith("shop:1")


def test_link_get_only_confirms_post_rolls_back_once(aws, lam, monkeypatch):
    _short(aws, monkeypatch, lam)
    for _ in range(3):
        lam.tick_once()
    token = _link_token(lam)
    page = lam.handler(_http("GET", "/r", token=token))
    assert page["statusCode"] == 200 and 'method="post"' in page["body"] and "shop:1" in page["body"]
    assert page["headers"]["cache-control"] == "no-store" and "frame-ancestors 'none'" in page["headers"]["content-security-policy"]
    assert current_td(aws).endswith("shop:2")  # GET 만으로는 아무 일도 없다(메일 검사기 · 미리보기)
    assert 'name="c"' in page["body"]
    wrong = lam.handler(_http("POST", "/r", body=f"t={token}&c=2"))  # 검사기가 양식을 대신 보내도(번호를 모름) 실행 안 됨
    assert wrong["statusCode"] == 400 and current_td(aws).endswith("shop:2")
    assert lam.handler(_http("POST", "/r", body=f"t={token}"))["statusCode"] == 400
    done = lam.handler(_http("POST", "/r", body=f"t={token}&c=1", b64=True))
    assert done["statusCode"] == 200 and current_td(aws).endswith("shop:1")
    again = lam.handler(_http("POST", "/r", body=f"t={token}&c=1"))
    assert again["statusCode"] == 410
    assert lam.handler(_http("GET", "/r", token=token))["statusCode"] == 410
    ev = watcher.events(aws, region=REGION, cluster="c", service="shop")
    assert ev[0]["kind"] == "rollback" and ev[0]["source"] == "link"
    outage = next(e for e in ev if e["kind"] == "outage")
    assert outage["actions"][0]["status"] == "used"
    assert watcher.offers(ev, current_td(aws)) == []


def test_link_refuses_when_version_changed_or_expired(aws, lam, monkeypatch):
    _short(aws, monkeypatch, lam)
    for _ in range(3):
        lam.tick_once()
    token = _link_token(lam)
    aws.client("ecs").register_task_definition(family="shop", containerDefinitions=[{"name": "app", "image": "app:v3", "memory": 128}])
    aws.client("ecs").update_service(cluster="c", service="shop", taskDefinition="shop:3")
    stale = lam.handler(_http("POST", "/r", body=f"t={token}&c=1"))
    assert stale["statusCode"] == 409 and current_td(aws).endswith("shop:3")
    # 만료 — 다시 v2 로 돌리고(새 장애 · 새 링크) 13시간 뒤에 연다
    aws.client("ecs").update_service(cluster="c", service="shop", taskDefinition="shop:2")
    lam.clock["t"] += 3600  # 30분 쉼이 지나게
    for _ in range(3):
        lam.tick_once()
    token2 = _link_token(lam)
    assert token2 != token
    lam.clock["t"] += 13 * 3600
    assert lam.handler(_http("POST", "/r", body=f"t={token2}&c=1"))["statusCode"] == 410
    assert current_td(aws).endswith("shop:2")


def test_http_rejects_junk(aws, lam):
    assert lam.handler(_http("GET", "/"))["statusCode"] == 200
    assert lam.handler(_http("GET", "/admin"))["statusCode"] == 404
    assert lam.handler(_http("DELETE", "/r"))["statusCode"] == 405
    assert lam.handler(_http("GET", "/r"))["statusCode"] == 400
    assert lam.handler(_http("GET", "/r", token="<script>alert(1)</script>"))["statusCode"] == 400
    assert lam.handler(_http("GET", "/r", token="A" * 43))["statusCode"] == 410  # 모양은 맞지만 없는 토큰
    assert lam.handler({"source": "aws.s3"}) == {"ignored": True}


def test_alarm_event_records_errors_and_ignores_others(aws, lam):
    def alarm(name, state="ALARM"):
        return {"source": "aws.cloudwatch", "detail-type": "CloudWatch Alarm State Change",
                "detail": {"alarmName": name, "state": {"value": state, "reason": "Threshold Crossed"}}}
    assert lam.handler(alarm("recoder-other-error-ratio")) == {"ignored": True}
    assert lam.handler(alarm(lam.names["alarms"][2], "OK")) == {"ignored": True}
    assert lam.handler(alarm(lam.names["alarms"][2]))["raised"] is True
    assert lam.handler(alarm(lam.names["alarms"][2]))["raised"] is False  # 30분 쉼
    assert lam.handler(alarm(lam.names["alarms"][0]))["raised"] is True  # 다른 종류는 따로
    kinds = [e["kind"] for e in watcher.events(aws, region=REGION, cluster="c", service="shop")]
    assert kinds == ["spike", "errors"]
    ev = watcher.events(aws, region=REGION, cluster="c", service="shop")
    assert ev[0]["actions"] == [] and ev[1]["actions"][0]["action"] == "rollback"  # 요청 급증은 버전 문제가 아니다


def test_ecs_events(aws, lam):
    svc = aws.client("ecs").describe_services(cluster="c", services=["shop"])["services"][0]
    task = {"source": "aws.ecs", "detail-type": "ECS Task State Change",
            "detail": {"group": "service:shop", "lastStatus": "STOPPED", "clusterArn": svc["clusterArn"],
                       "stopCode": "EssentialContainerExited", "stoppedReason": "Essential container in task exited",
                       "containers": [{"name": "app", "exitCode": 1}], "taskDefinitionArn": td(2)}}
    assert lam.handler({**task, "detail": {**task["detail"], "stopCode": "ServiceSchedulerInitiated"}}) == {"ignored": True}
    assert lam.handler({**task, "detail": {**task["detail"], "group": "service:other"}}) == {"ignored": True}
    assert lam.handler(task)["raised"] is True
    svc_event = {"source": "aws.ecs", "detail-type": "ECS Service Action", "resources": [svc["serviceArn"]],
                 "detail": {"eventName": "SERVICE_TASK_START_IMPAIRED"}}
    assert lam.handler(svc_event)["raised"] is True
    assert lam.handler({**svc_event, "resources": ["arn:aws:ecs:us-east-1:1:service/c/other"]}) == {"ignored": True}
    dep = {"source": "aws.ecs", "detail-type": "ECS Deployment State Change", "resources": [svc["serviceArn"]],
           "detail": {"eventName": "SERVICE_DEPLOYMENT_FAILED", "reason": "circuit breaker"}}
    assert lam.handler(dep)["raised"] is True
    kinds = [e["kind"] for e in watcher.events(aws, region=REGION, cluster="c", service="shop")]
    assert kinds == ["deploy_failed", "impaired", "crash"]
    crash = watcher.events(aws, region=REGION, cluster="c", service="shop")[2]
    assert "종료 코드 1" in crash["detail"] and crash["task_definition"] == "shop:2"


def test_no_link_when_current_is_already_stable(aws, lam, monkeypatch):
    aws.client("ecs").update_service(cluster="c", service="shop", taskDefinition="shop:1")
    _short(aws, monkeypatch, lam)
    for _ in range(3):
        lam.tick_once()
    assert "/r?t=" not in lam.posted[-1]["content"]
    assert watcher.events(aws, region=REGION, cluster="c", service="shop")[0]["actions"] == []


def test_mail_goes_to_sns_topic(aws, lam, monkeypatch):
    sent = []
    real = lam._client

    def spy(name):
        c = real(name)
        if name != "sns":
            return c

        class Wrap:
            def publish(self, **kw):
                sent.append(kw)
                return c.publish(**kw)
        return Wrap()
    monkeypatch.setattr(lam, "_client", spy)
    _short(aws, monkeypatch, lam)
    for _ in range(3):
        lam.tick_once()
    assert sent and sent[0]["TopicArn"].endswith(":recoder-shop-alerts")
    assert sent[0]["Subject"].isascii() and len(sent[0]["Subject"]) < 100 and "/r?t=" in sent[0]["Message"]


def test_offer_without_link_still_shows_in_vscode(aws, lam, monkeypatch):
    """조직 정책이 공개 URL 을 막아도(링크 없음) 롤백 제안은 VS Code 모니터에 남는다."""
    aws.client("dynamodb").update_item(TableName=lam.names["table"], Key={"pk": {"S": "CONFIG"}, "sk": {"S": "CONFIG"}},
                                       UpdateExpression="SET #u = :e", ExpressionAttributeNames={"#u": "url"},
                                       ExpressionAttributeValues={":e": {"S": ""}})
    _short(aws, monkeypatch, lam)
    for _ in range(3):
        lam.tick_once()
    assert "/r?t=" not in lam.posted[-1]["content"]
    ev = watcher.events(aws, region=REGION, cluster="c", service="shop")
    offers = watcher.offers(ev, current_td(aws))
    assert len(offers) == 1 and offers[0]["target"].endswith("shop:1")
    out = watcher.rollback(aws, region=REGION, cluster="c", service="shop", target_td=offers[0]["target"],
                           from_td=offers[0]["from_td"], approved=True)
    assert out["ok"] and current_td(aws).endswith("shop:1")


def test_health_check_kills_count_as_crash(aws, lam):
    svc = aws.client("ecs").describe_services(cluster="c", services=["shop"])["services"][0]
    ev = {"source": "aws.ecs", "detail-type": "ECS Task State Change",
          "detail": {"group": "service:shop", "lastStatus": "STOPPED", "clusterArn": svc["clusterArn"],
                     "stopCode": "ServiceSchedulerInitiated", "stoppedReason": "Task failed ELB health checks in (target-group x)"}}
    assert lam.handler(ev)["raised"] is True
    assert "헬스 체크" in watcher.events(aws, region=REGION, cluster="c", service="shop")[0]["detail"]


def test_failed_record_releases_cooldown(aws, lam, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("table down")
    real = lam._record
    monkeypatch.setattr(lam, "_record", boom)
    with pytest.raises(RuntimeError):
        lam.raise_event("errors", "x", source="alarm")
    monkeypatch.setattr(lam, "_record", real)
    assert lam.raise_event("errors", "x", source="alarm") is not None  # 쉼에 막히지 않는다


def test_duplicate_schedule_delivery_counts_once(aws, lam):
    assert lam.tick_once().get("duplicate") is None
    again = lam.handler({"source": "aws.events"})  # 같은 시각에 한 번 더 배달
    assert again.get("duplicate") is True
    assert watcher.status(aws, region=REGION, cluster="c", service="shop")["ticks"] == 1


def test_missing_service_is_reported_once_a_day(aws, lam):
    aws.client("ecs").update_service(cluster="c", service="shop", desiredCount=0)
    aws.client("ecs").delete_service(cluster="c", service="shop")
    first = lam.tick_once()
    assert first["missing"] and first["raised"]
    assert lam.tick_once()["raised"] is False
    lam.clock["t"] += 25 * 3600
    assert lam.tick_once()["raised"] is True


def test_alarm_mail_only_when_it_adds_a_link(aws, lam, monkeypatch):
    sent = []
    real = lam._client

    def spy(name):
        c = real(name)
        if name != "sns":
            return c

        class Wrap:
            def publish(self, **kw):
                sent.append(kw)
                return c.publish(**kw)
        return Wrap()
    monkeypatch.setattr(lam, "_client", spy)
    alarm = {"source": "aws.cloudwatch", "detail": {"state": {"value": "ALARM"}}}
    lam.handler({**alarm, "detail": {**alarm["detail"], "alarmName": lam.names["alarms"][0]}})  # 요청 급증 — AWS 경보가 이미 메일
    assert sent == []
    lam.handler({**alarm, "detail": {**alarm["detail"], "alarmName": lam.names["alarms"][2]}})  # 오류 증가 — 롤백 링크 추가
    assert len(sent) == 1 and "/r?t=" in sent[0]["Message"]
    assert len(lam.posted) == 2  # 디스코드에는 둘 다


def test_vscode_rollback_only_to_the_offered_version(aws):
    installed(aws)
    aws.client("ecs").register_task_definition(family="shop", containerDefinitions=[{"name": "app", "image": "app:v3", "memory": 128}])
    aws.client("ecs").update_service(cluster="c", service="shop", taskDefinition="shop:3")
    with pytest.raises(watcher.WatchError, match="제안한 이전 버전"):
        watcher.rollback(aws, region=REGION, cluster="c", service="shop", target_td=td(2), from_td=td(3), approved=True)
    assert current_td(aws).endswith("shop:3")


# ---------------------------------------------------------------------------
# 정리(aws_cleanup) 와 API
# ---------------------------------------------------------------------------

def test_cleanup_finds_and_deletes_watcher_first(aws):
    import aws_cleanup
    installed(aws)
    clients = aws_cleanup.make_clients(aws)
    items, gaps = aws_cleanup.find(clients, region=REGION, records=[{"cluster": "c", "service": "shop", "region": REGION}])
    kinds = [i.kind for i in items]
    assert "watcher" in kinds and kinds.index("watcher") < kinds.index("ecs_service")
    w = next(i for i in items if i.kind == "watcher")
    assert w.tagged
    out = aws_cleanup.delete(clients, [w.id], region=REGION, approved=True,
                             records=[{"cluster": "c", "service": "shop", "region": REGION}], sleep=lambda _: None)
    assert out["deleted"] == 1, out
    assert watcher.status(aws, region=REGION, cluster="c", service="shop") == {"installed": False}


def test_cleanup_finds_watcher_left_after_service_was_deleted(aws):
    import aws_cleanup
    installed(aws)
    aws.client("ecs").update_service(cluster="c", service="shop", desiredCount=0)
    aws.client("ecs").delete_service(cluster="c", service="shop")
    clients = aws_cleanup.make_clients(aws)
    items, _ = aws_cleanup.find(clients, region=REGION, records=[{"cluster": "c", "service": "shop", "region": REGION}])
    w = [i for i in items if i.kind == "watcher"]
    assert len(w) == 1 and w[0].tagged
    out = aws_cleanup.delete(clients, [w[0].id], region=REGION, approved=True,
                             records=[{"cluster": "c", "service": "shop", "region": REGION}], sleep=lambda _: None)
    assert out["deleted"] == 1, out


def test_routes_require_registered_service_and_approval(aws, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import ops_alerts
    from api.routes import ops_watch
    monkeypatch.setattr(ops_watch, "session_for", lambda region: aws)
    app = FastAPI()
    app.include_router(ops_watch.router)
    c = TestClient(app)
    assert c.get("/api/ops/watch/overview").json() == {"monitors": [], "current": None}
    ops_alerts.register(region=REGION, cluster="c", service="shop", log_group="/ecs/shop", metrics=False)
    key = f"{REGION}/c/shop"
    assert c.post("/api/ops/watch/install", json={"key": "us-east-1/c/ghost", "approved": True}).status_code == 404
    assert c.post("/api/ops/watch/install", json={"key": key}).status_code == 400
    plan = c.post("/api/ops/watch/plan", json={"key": key}).json()
    assert plan["role"]["exists"] is True
    done = c.post("/api/ops/watch/install", json={"key": key, "approved": True})
    assert done.status_code == 200 and done.json()["installed"]
    assert any("다음 배포부터" in n for n in done.json()["notes"])  # 배포 기록이 없으면 링크는 다음 배포부터
    ov = c.get("/api/ops/watch/overview", params={"key": key}).json()
    cur = ov["current"]
    assert cur["service_state"]["revision"] == "shop:2" and cur["watcher"]["installed"] and cur["series"] is None
    bad = c.post("/api/ops/watch/rollback", json={"key": key, "target_td": td(1), "from_td": td(7), "approved": True})
    assert bad.status_code == 400
    assert c.post("/api/ops/watch/rollback", json={"key": key, "target_td": td(1), "from_td": td(2)}).status_code == 400
    assert current_td(aws).endswith("shop:2")
    assert c.post("/api/ops/watch/settings", json={"key": key, "discord": "nope"}).status_code == 400
    assert c.post("/api/ops/watch/settings", json={"key": key, "discord": "nope", "approved": True}).status_code == 400
    assert c.post("/api/ops/watch/uninstall", json={"key": key, "approved": True}).json()["ok"]
