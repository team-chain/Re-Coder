"""3.0.2 — 롤백 대상은 마지막 **성공** 배포 · 감시자 링크는 확인 뒤에만 소비 · VS Code 롤백은 배포 잠금을 따른다.

AWS 는 moto 로 흉내 낸다(실제 계정 · 키를 쓰지 않는다).
"""
from __future__ import annotations

import asyncio

import pytest

import ecs_stable
import watcher
from core.agents.ecs_agent import ECSAgent
from core.schemas import ECSDeployRecord, ECSDeployRequest, ECSDeployStatus

# test_watcher 의 moto 픽스처·도우미만 가져온다(그 파일의 테스트는 다시 모으지 않는다).
from tests.test_watcher import REGION, _http, _link_token, _short, aws, current_td, installed, lam, td  # noqa: F401


def _req(image: str) -> ECSDeployRequest:
    return ECSDeployRequest(project_id="p", cluster="c", service="shop", image=image, region=REGION,
                            task_definition_family="shop", run_preflight=False, run_security_scan=False, generate_sbom=False, url_wait_timeout=0)


def _clients(s) -> dict:
    return {name: s.client(name) for name in ("ecs", "sts", "dynamodb", "iam")}


def _deploy(agent: ECSAgent, s, image: str, *, ok: bool) -> tuple[str, str]:
    """파이프라인의 등록 → 서비스 갱신 → (성공이면 기록) → 감시자 갱신만 실제 코드로 돌린다."""
    req = _req(image)
    clients = _clients(s)
    arn, prev = asyncio.run(agent._step_register_task_definition(req, clients, image))
    s.client("ecs").update_service(cluster="c", service="shop", taskDefinition=arn)
    rec = ECSDeployRecord(project_id="p", cluster="c", service="shop", region=REGION)
    rec.task_definition_arn, rec.previous_task_definition_arn = arn, prev
    if ok:
        rec.status = ECSDeployStatus.SUCCEEDED
        agent._record_success(req, rec)
    else:
        rec.status = ECSDeployStatus.FAILED
    asyncio.run(agent._tell_watcher(req, rec, clients))
    return arn, prev or ""


def _rev(arn: str) -> str:
    return arn.rsplit("/", 1)[-1]


def test_rollback_target_is_last_successful_deploy_never_a_failed_one(aws):
    installed(aws)  # 감시자 설치(밤 롤백 링크의 기준 stable_td 를 확인한다)
    # 이 PC 에서 v1, v2 를 정상 배포했다.
    ecs_stable.record_success(REGION, "c", "shop", td(1))
    ecs_stable.record_success(REGION, "c", "shop", td(2))
    agent = ECSAgent()

    v3, prev3 = _deploy(agent, aws, "app:v3", ok=False)
    assert _rev(v3) == "shop:3"
    assert _rev(prev3) == "shop:2", "v2 정상 → v3 실패: 되돌릴 곳은 v2"
    assert watcher.status(aws, region=REGION, cluster="c", service="shop")["stable"] == "shop:2"

    # 서비스는 지금 실패한 v3 를 가리킨다. 예전에는 이것이 다음 배포의 '이전 버전'이 됐다.
    assert current_td(aws).endswith("shop:3")
    v4, prev4 = _deploy(agent, aws, "app:v4", ok=True)
    assert _rev(prev4) == "shop:2", "v4 의 롤백 대상이 실패한 v3 이면 안 된다"
    st = watcher.status(aws, region=REGION, cluster="c", service="shop")
    assert st["stable"] == "shop:2", "v4 가 도는 동안 밤 롤백 링크는 v4 직전의 정상 버전(v2)으로"

    v5, prev5 = _deploy(agent, aws, "app:v5", ok=False)
    assert _rev(prev5) == "shop:4", "v4 정상 → v5 실패: 되돌릴 곳은 v4(실패한 v3 아님)"
    assert watcher.status(aws, region=REGION, cluster="c", service="shop")["stable"] == "shop:4"
    assert [_rev(t) for t in ecs_stable.history(REGION, "c", "shop")] == ["shop:1", "shop:2", "shop:4"]


def test_without_local_record_only_a_steady_service_counts():
    td_ = "arn:aws:ecs:us-east-1:1:task-definition/app:7"
    steady = {"status": "ACTIVE", "taskDefinition": td_,
              "deployments": [{"status": "PRIMARY", "taskDefinition": td_, "rolloutState": "COMPLETED"}]}
    failed = {"status": "ACTIVE", "taskDefinition": td_,
              "deployments": [{"status": "PRIMARY", "taskDefinition": td_, "rolloutState": "FAILED"}]}
    rolling = {"status": "ACTIVE", "taskDefinition": td_,
               "deployments": [{"status": "PRIMARY", "taskDefinition": td_, "desiredCount": 1, "runningCount": 0},
                               {"status": "ACTIVE", "taskDefinition": "x:6"}]}
    assert ecs_stable.steady_task_definition(steady) == td_
    assert ecs_stable.steady_task_definition(failed) == ""
    assert ecs_stable.steady_task_definition(rolling) == ""
    assert ecs_stable.steady_task_definition(None) == ""
    req = ECSDeployRequest(project_id="p", cluster="c9", service="none", image="i", region=REGION)
    assert ECSAgent._rollback_target(req, "arn:...:task-definition/app:8", "") is None
    assert ECSAgent._rollback_target(req, "arn:...:task-definition/app:8", td_) == td_
    assert ECSAgent._rollback_target(req, td_, td_) is None, "새 리비전 자신으로는 되돌리지 않는다"


def test_watcher_install_uses_last_success_not_previous_of_record(aws, monkeypatch):
    from api.routes import ops_watch
    ecs_stable.record_success(REGION, "c", "shop", td(1))
    assert ops_watch._stable_from_records({"region": REGION, "cluster": "c", "service": "shop"}, td(2)) == td(1)
    assert ops_watch._stable_from_records({"region": REGION, "cluster": "c", "service": "shop"}, td(1)) == ""


# ---------------------------------------------------------------------------
# 승인 링크 — 확인 전에 소비하지 않는다
# ---------------------------------------------------------------------------

def _link(aws, lam, monkeypatch) -> str:
    _short(aws, monkeypatch, lam)
    for _ in range(3):
        lam.tick_once()
    return _link_token(lam)


def test_link_stays_usable_when_describe_services_fails(aws, lam, monkeypatch):
    token = _link(aws, lam, monkeypatch)
    working = lam._service

    def broken():
        raise RuntimeError("Throttling")
    monkeypatch.setattr(lam, "_service", broken)
    page = lam.handler(_http("POST", "/r", body=f"t={token}&c=1"))
    assert page["statusCode"] == 502 and "아직 쓸 수 있습니다" in page["body"]
    assert current_td(aws).endswith("shop:2")
    ev = watcher.events(aws, region=REGION, cluster="c", service="shop")
    assert ev[0]["kind"] == "rollback" and "확인하지 못했습니다" in ev[0]["detail"]
    monkeypatch.setattr(lam, "_service", working)
    done = lam.handler(_http("POST", "/r", body=f"t={token}&c=1"))
    assert done["statusCode"] == 200 and current_td(aws).endswith("shop:1")
    assert lam.handler(_http("POST", "/r", body=f"t={token}&c=1"))["statusCode"] == 410


def test_link_reverts_to_pending_when_update_service_fails(aws, lam, monkeypatch):
    token = _link(aws, lam, monkeypatch)
    real_client = lam._client
    state = {"fail": True}

    class FailingEcs:
        def __init__(self, inner):
            self.inner = inner

        def update_service(self, **kw):
            if state["fail"]:
                raise RuntimeError("boom")
            return self.inner.update_service(**kw)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(lam, "_client", lambda name: FailingEcs(real_client(name)) if name == "ecs" else real_client(name))
    page = lam.handler(_http("POST", "/r", body=f"t={token}&c=1"))
    assert page["statusCode"] == 502 and "같은 링크로 다시 시도" in page["body"]
    assert current_td(aws).endswith("shop:2")
    ev = watcher.events(aws, region=REGION, cluster="c", service="shop")
    assert ev[0]["kind"] == "rollback" and "롤백을 시작하지 못했습니다" in ev[0]["detail"]
    state["fail"] = False
    done = lam.handler(_http("POST", "/r", body=f"t={token}&c=1"))
    assert done["statusCode"] == 200 and current_td(aws).endswith("shop:1")


# ---------------------------------------------------------------------------
# VS Code 롤백 · 알림 설정 라우트
# ---------------------------------------------------------------------------

@pytest.fixture
def client(aws, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import ops_alerts
    from api.routes import ops_watch
    monkeypatch.setattr(ops_watch, "session_for", lambda region: aws)
    ops_alerts.register(region=REGION, cluster="c", service="shop", log_group="/ecs/shop", metrics=False)
    app = FastAPI()
    app.include_router(ops_watch.router)
    installed(aws)
    return TestClient(app), f"{REGION}/c/shop"


def test_vscode_rollback_returns_409_while_a_deploy_runs(client, aws):
    from api.routes import ecs as ecs_routes
    c, key = client
    running = ECSDeployRecord(project_id="p", cluster="c", service="shop", region=REGION,
                              status=ECSDeployStatus.IN_PROGRESS)
    ecs_routes._deploy_records[running.deployment_id] = running
    try:
        body = {"key": key, "target_td": td(1), "from_td": td(2), "approved": True}
        resp = c.post("/api/ops/watch/rollback", json=body)
        assert resp.status_code == 409 and "배포가 진행 중" in resp.json()["detail"]
        assert current_td(aws).endswith("shop:2")
        running.status = ECSDeployStatus.SUCCEEDED
        ok = c.post("/api/ops/watch/rollback", json=body)
        assert ok.status_code == 200 and current_td(aws).endswith("shop:1")
    finally:
        ecs_routes._deploy_records.pop(running.deployment_id, None)


def test_watch_settings_require_approval(client, aws):
    c, key = client
    hook = "https://discord.com/api/webhooks/123456/abcdefghijklmnopqrstuvwxyz"
    refused = c.post("/api/ops/watch/settings", json={"key": key, "discord": hook})
    assert refused.status_code == 400 and "승인" in refused.json()["detail"]
    assert watcher.status(aws, region=REGION, cluster="c", service="shop")["discord"] is False
    ok = c.post("/api/ops/watch/settings", json={"key": key, "discord": hook, "approved": True})
    assert ok.status_code == 200
    assert watcher.status(aws, region=REGION, cluster="c", service="shop")["discord"] is True


def test_aws_errors_do_not_leak_raw_text(client, aws, monkeypatch):
    from api.routes import ops_watch
    c, key = client

    def boom(*a, **k):
        raise RuntimeError("arn:aws:iam::123456789012:role/secret-internal-detail")
    monkeypatch.setattr(ops_watch.watcher, "plan", boom)
    resp = c.post("/api/ops/watch/plan", json={"key": key})
    assert resp.status_code == 502
    assert "secret-internal-detail" not in resp.text and "오류 ID" in resp.json()["detail"]
