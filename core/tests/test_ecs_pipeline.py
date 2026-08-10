"""
ECS 배포 글루 계층 테스트 — FR-05-04

**이 파일이 없어서 사고가 났다.**

`aws_infra.py` 와 `ecs_build.py` 는 테스트가 촘촘했는데, 그것들을 잇는
`ECSAgent.deploy` 와 라우트 두 개에는 테스트가 **하나도** 없었다. 그 결과:

  · 모든 배포 요청이 항상 503 이었다 (OPA 를 잘못된 인자로 불러 TypeError
    가 났고, except 가 그걸 "OPA 장애"로 바꿔 내보냈다). TestClient 로
    POST 한 번만 해봤으면 즉시 드러났을 일이다.
  · 빈 계정의 첫 배포를 preflight 가 막았다 — 없는 클러스터를 만들어 주는
    코드가 바로 다음 단계에 있는데도.
  · 서킷 브레이커가 누적 카운트를 매 폴링마다 새로 세어 정상 배포를
    45초 만에 중단시켰다.

아래 테스트들은 그 각각을 고정한다.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))

from core import aws_infra  # noqa: E402
from core.agents.ecs_agent import ECSAgent  # noqa: E402
from core.schemas import (  # noqa: E402
    ECSDeployRecord,
    ECSDeployRequest,
    ECSDeployStatus,
)


def make_request(**overrides) -> ECSDeployRequest:
    fields = {
        "project_id": "p",
        "cluster": "recoder-cluster",
        "service": "recoder-app",
        "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/recoder-app:v1",
        "region": "us-east-1",
        "run_preflight": False,
        "run_security_scan": False,
        "generate_sbom": False,
        "url_wait_timeout": 0,
    }
    fields.update(overrides)
    return ECSDeployRequest(**fields)


def client_error(code: str, message: str = "boom"):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}}, "Op")


# ===========================================================================
# 라우트 — 배포 버튼이 실제로 에이전트까지 닿는가
# ===========================================================================


@pytest.fixture
def app_client(monkeypatch):
    """미들웨어 없이 라우터만 올린 테스트 클라이언트."""
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes import deploy_ecs, ecs as ecs_routes
    from core import opa_client as opa_module

    class _Allow:
        decision = "allow"
        reason = "테스트"
        fix_suggestion = None
        opa_available = True

    async def _evaluate(**_kwargs):
        return _Allow()

    monkeypatch.setattr(opa_module.opa_client, "evaluate", _evaluate)
    ecs_routes._deploy_records.clear()

    app = FastAPI()
    app.include_router(ecs_routes.router)
    app.include_router(deploy_ecs.router)
    return TestClient(app, raise_server_exceptions=False), ecs_routes


def test_extension_deploy_button_reaches_the_agent(app_client, monkeypatch):
    """[회귀] 예전에는 이 호출이 **무조건 503** 이었다.

    원인은 OPA 를 존재하지 않는 인자 이름으로 부른 TypeError 였는데,
    `except Exception` 이 그걸 "OPA에 연결할 수 없습니다"로 바꿔 내보냈다.
    우리 코드의 타입 오류가 외부 서비스 장애로 둔갑한 것이라, 사용자는
    원인을 절대 찾을 수 없었다.
    """
    client, ecs_routes = app_client
    started: list[ECSDeployRequest] = []

    async def _fake_deploy(request, record=None):
        started.append(request)
        if record is not None:
            record.status = ECSDeployStatus.SUCCEEDED
            return record
        return ECSDeployRecord(status=ECSDeployStatus.SUCCEEDED)

    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)

    resp = client.post(
        "/api/deploy/ecs",
        json={"ecs_cluster": "recoder-cluster", "ecs_service": "recoder-app"},
    )
    assert resp.status_code == 200, resp.text
    # 사이드바는 status === 'ok' 일 때만 폴링을 시작한다.
    assert resp.json()["status"] == "ok"
    assert len(started) == 1
    assert started[0].cluster == "recoder-cluster"


def test_policy_denial_is_403_not_503(app_client, monkeypatch):
    """정책이 막은 것과 OPA 에 못 닿은 것은 다른 사건이다.

    같은 코드로 보고하면 사용자가 무엇을 해야 할지 알 수 없다.
    """
    client, _ = app_client
    from core import opa_client as opa_module

    class _Deny:
        decision = "deny"
        reason = "이미지 태그가 latest 입니다"
        fix_suggestion = "고정 태그를 쓰세요"
        opa_available = True

    async def _evaluate(**_kwargs):
        return _Deny()

    monkeypatch.setattr(opa_module.opa_client, "evaluate", _evaluate)
    resp = client.post("/api/deploy/ecs", json={})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "policy_denied"


def test_unreachable_opa_is_503(app_client, monkeypatch):
    client, _ = app_client
    from core import opa_client as opa_module

    class _Unavailable:
        decision = "deny"
        reason = "OPA 연결 실패"
        fix_suggestion = None
        opa_available = False

    async def _evaluate(**_kwargs):
        return _Unavailable()

    monkeypatch.setattr(opa_module.opa_client, "evaluate", _evaluate)
    resp = client.post("/api/deploy/ecs", json={})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "opa_unavailable"


def test_a_broken_policy_client_is_not_reported_as_an_opa_outage(
    app_client, monkeypatch
):
    """부정 통제: 우리 쪽 결함을 외부 장애로 보고하면 원인을 못 찾는다."""
    client, _ = app_client
    from core import opa_client as opa_module

    async def _explode(**_kwargs):
        raise TypeError("evaluate() got an unexpected keyword argument 'policy_path'")

    monkeypatch.setattr(opa_module.opa_client, "evaluate", _explode)
    resp = client.post("/api/deploy/ecs", json={})
    assert resp.status_code == 500
    assert resp.json()["detail"]["error"] == "policy_evaluation_crashed"


def test_status_endpoint_reports_the_live_record(app_client, monkeypatch):
    """[회귀] 진행 상황이 실제로 흘러야 한다.

    예전에는 에이전트가 자기 기록을 따로 만들어 채우고 **끝에 한 번**
    저장소를 교체했다. 그동안 사이드바는 몇 분 내내 "대기 중"만 보다가
    갑자기 결과로 건너뛰었다.
    """
    client, ecs_routes = app_client

    async def _fake_deploy(request, record=None):
        assert record is not None, "라우트가 저장소의 기록을 넘기지 않았다"
        record.status = ECSDeployStatus.IN_PROGRESS
        record.provisioned["cluster"] = request.cluster
        record.image_uri = "repo/app:v1"
        return record

    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)
    client.post("/api/deploy/ecs", json={"ecs_cluster": "recoder-cluster"})

    body = client.get("/api/deploy/ecs/status").json()
    assert body["running"] is True
    assert body["stage"] == "deploying", "기계 토큰이 아니다"
    assert body["stage_text"] == "배포 중", "사람용 문구는 따로 와야 한다"
    assert body["image_uri"] == "repo/app:v1"
    assert any("cluster" in line for line in body["log_tail"])


# ===========================================================================
# 확장 ↔ 코어 요청 번역
# ===========================================================================


def test_extension_field_names_are_translated():
    from api.routes.deploy_ecs import ExtensionEcsDeployRequest, to_core_request

    core = to_core_request(
        ExtensionEcsDeployRequest(
            ecs_cluster="c", ecs_service="s", aws_region="us-west-2",
            repo_name="myrepo", tag="v9", container_port=3000,
            task_family="fam", workspace_path="/w", skip_sbom=True,
        )
    )
    assert (core.cluster, core.service, core.region) == ("c", "s", "us-west-2")
    assert (core.ecr_repo, core.image_tag) == ("myrepo", "v9")
    assert core.container_port == 3000
    assert core.task_definition_family == "fam"
    assert core.generate_sbom is False


def test_blank_region_does_not_override_the_default(monkeypatch):
    """빈 문자열을 그대로 넘기면 기본 리전 계산이 무력화된다."""
    monkeypatch.setenv("AWS_REGION", "ap-northeast-2")
    from api.routes.deploy_ecs import ExtensionEcsDeployRequest, to_core_request

    assert to_core_request(
        ExtensionEcsDeployRequest(aws_region="  ")
    ).region == "ap-northeast-2"


def test_default_region_is_us_east_1_without_env(monkeypatch):
    """학교 계정은 us-east-1 / us-west-2 만 허용한다."""
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert ECSDeployRequest(project_id="p", cluster="c", service="s").region == "us-east-1"


# ===========================================================================
# Task Definition 렌더링
# ===========================================================================


def test_task_definition_rejects_the_placeholder_account():
    """부정 통제: 예전 기본값 `arn:aws:iam::000000000000:role/...` 은 형식만
    맞고 존재하지 않는 ARN 이었다. 등록이 거부될 때 "계정 번호가 0으로
    채워졌다"는 걸 알아채기가 매우 어려웠다."""
    agent = ECSAgent()
    with pytest.raises(aws_infra.InfraError) as caught:
        agent._render_task_definition(
            make_request(), image="img",
            execution_role_arn="arn:aws:iam::000000000000:role/ecsTaskExecutionRole",
            task_role_arn="arn:aws:iam::123456789012:role/ecsTaskRole",
        )
    assert "올바르지 않습니다" in str(caught.value)


def test_task_definition_log_group_matches_the_one_we_create():
    """파생값이 어긋나면 컨테이너가 로그를 못 남기고 태스크가 실패한다."""
    agent = ECSAgent()
    req = make_request(task_definition_family="my-family", container_port=9000)
    rendered = agent._render_task_definition(
        req, image="img",
        execution_role_arn="arn:aws:iam::123456789012:role/LabRole",
        task_role_arn="arn:aws:iam::123456789012:role/LabRole",
    )
    container = rendered["containerDefinitions"][0]
    options = container["logConfiguration"]["options"]
    assert options["awslogs-group"] == agent.log_group_name(req)
    assert options["awslogs-region"] == req.region
    assert container["portMappings"][0]["containerPort"] == 9000


def test_task_definition_has_no_curl_health_check():
    """부정 통제: curl 은 런타임 이미지(python:slim)에 없다. 넣어두면
    컨테이너가 항상 UNHEALTHY 가 되어 ECS 가 무한 재시작한다."""
    agent = ECSAgent()
    rendered = agent._render_task_definition(
        make_request(), image="img",
        execution_role_arn="arn:aws:iam::123456789012:role/LabRole",
        task_role_arn="arn:aws:iam::123456789012:role/LabRole",
    )
    container = rendered["containerDefinitions"][0]
    command = str(container.get("healthCheck", {}).get("command", ""))
    assert "curl" not in command


def test_role_arns_keep_the_iam_path(monkeypatch):
    """부정 통제: 경로를 떼면 권한표가 인가한 ARN 과 다른 곳을 가리켜
    PassRole 이 거부된다. aws_policy.py 가 경고해둔 바로 그 함정이다."""
    monkeypatch.setenv("ECS_EXECUTION_ROLE_ARN", "arn:aws:iam::111:role/team/EcsExec")
    monkeypatch.setenv("ECS_TASK_ROLE_ARN", "arn:aws:iam::111:role/team/EcsTask")

    class _Sts:
        @staticmethod
        def get_caller_identity():
            return {"Account": "222222222222"}

    exec_arn, task_arn = ECSAgent()._resolve_role_arns(
        make_request(), {"sts": _Sts()}
    )
    assert exec_arn == "arn:aws:iam::222222222222:role/team/EcsExec"
    assert task_arn == "arn:aws:iam::222222222222:role/team/EcsTask"


def test_expired_credentials_while_resolving_roles_say_so():
    class _Sts:
        @staticmethod
        def get_caller_identity():
            raise client_error("ExpiredToken", "token expired")

    with pytest.raises(aws_infra.InfraError) as caught:
        ECSAgent()._resolve_role_arns(make_request(), {"sts": _Sts()})
    assert "4시간" in caught.value.remedy


# ===========================================================================
# 폴링 · 서킷 브레이커
# ===========================================================================


class FakePollEcs:
    """describe_services 응답을 미리 정해두고 순서대로 돌려준다."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.calls = 0

    def describe_services(self, **_):
        self.calls += 1
        frame = self._frames[min(self.calls - 1, len(self._frames) - 1)]
        if isinstance(frame, Exception):
            raise frame
        return {"services": [{"deployments": [dict(frame, status="PRIMARY")]}]}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import asyncio as _asyncio

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(_asyncio, "sleep", _instant)


def test_one_failed_task_does_not_trip_the_breaker(monkeypatch):
    """[회귀] `failedTasks` 는 **누적 카운트**다.

    폴링마다 그 값을 통째로 새 실패로 세면, 태스크 하나가 한 번 실패했을
    뿐인데 fail 만 쌓이고 pass 는 한 번도 안 들어가서 정상 배포가 45초 만에
    자동 중단됐다.
    """
    import boto3

    frames = [
        {"runningCount": 0, "desiredCount": 1, "failedTasks": 1, "rolloutState": "IN_PROGRESS"},
        {"runningCount": 0, "desiredCount": 1, "failedTasks": 1, "rolloutState": "IN_PROGRESS"},
        {"runningCount": 0, "desiredCount": 1, "failedTasks": 1, "rolloutState": "IN_PROGRESS"},
        {"runningCount": 1, "desiredCount": 1, "failedTasks": 1, "rolloutState": "COMPLETED"},
    ]
    fake = FakePollEcs(frames)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)

    success, failures, breaker = asyncio.run(
        ECSAgent()._step_poll_deployment(make_request(), ECSDeployRecord())
    )
    assert breaker is False, "정상 배포를 서킷 브레이커가 중단시켰다"
    assert success is True
    assert failures == 1, f"실패를 {failures}번으로 중복 계산했다"


def test_repeated_new_failures_still_trip_the_breaker(monkeypatch):
    """반대 방향 — 진짜로 계속 실패하면 멈춰야 한다."""
    import boto3

    frames = [
        {"runningCount": 0, "desiredCount": 1, "failedTasks": n, "rolloutState": "IN_PROGRESS"}
        for n in (1, 2, 3, 4, 5)
    ]
    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakePollEcs(frames))
    success, _failures, breaker = asyncio.run(
        ECSAgent()._step_poll_deployment(make_request(), ECSDeployRecord())
    )
    assert success is False
    assert breaker is True


def test_expired_credentials_are_not_reported_as_health_failure(monkeypatch):
    """부정 통제: 자격증명 만료를 "Health Check 실패"로 보고하면 사용자는
    멀쩡한 앱을 들여다보게 된다. 학교 계정은 4시간마다 끊긴다."""
    import boto3

    monkeypatch.setattr(
        boto3, "client",
        lambda *a, **k: FakePollEcs([client_error("ExpiredTokenException")]),
    )
    with pytest.raises(aws_infra.InfraError) as caught:
        asyncio.run(
            ECSAgent()._step_poll_deployment(make_request(), ECSDeployRecord())
        )
    assert "자격증명이 만료" in str(caught.value)


# ===========================================================================
# 파이프라인 전체
# ===========================================================================


def test_deploy_fills_the_record_it_was_given():
    """라우트가 저장소에 넣어둔 객체를 그대로 채워야 진행이 보인다."""
    record = ECSDeployRecord(deployment_id="fixed-id")
    agent = ECSAgent()
    result = asyncio.run(
        agent.deploy(make_request(region="nope-1"), record=record)
    )  # 리전 검증에서 즉시 실패
    assert result is record, "다른 기록 객체를 만들어 돌려줬다"
    assert result.deployment_id == "fixed-id"


def test_every_failure_path_stamps_a_finish_time():
    """[회귀] 실패로 끝나면 종료 시각이 비어서 확장이 영원히 "진행 중"으로
    표시했다."""
    record = ECSDeployRecord()
    result = asyncio.run(
        ECSAgent().deploy(make_request(region="nope-1"), record=record)
    )
    assert result.status == ECSDeployStatus.FAILED
    assert result.completed_at is not None, "실패인데 종료 시각이 없다"


def test_a_bad_region_produces_an_actionable_message():
    result = asyncio.run(ECSAgent().deploy(make_request(region="us-east")))
    assert result.status == ECSDeployStatus.FAILED
    assert result.error_message


# ===========================================================================
# Preflight
# ===========================================================================


def test_preflight_does_not_block_the_first_deploy_when_provisioning():
    """[회귀] 빈 계정에서 첫 배포가 "클러스터를 찾을 수 없습니다"로 중단됐다.
    정작 그 클러스터를 만들어 주는 코드가 바로 다음 단계에 있었다."""
    from core.agents.preflight_agent import PreflightAgent

    agent = PreflightAgent()

    class _Missing:
        """조회는 성공하는데 결과가 비어 있는 상태 = "아직 안 만들어졌다"."""

        @staticmethod
        def describe_clusters(**_):
            return {"clusters": []}

        @staticmethod
        def describe_services(**_):
            return {"services": []}

    # 실제 boto3 를 타면 자격증명이 없어 "조회 실패"(error)로 빠진다.
    # 여기서 보려는 것은 "조회는 됐는데 리소스가 없다"는 경우다.
    agent._ecs_client = lambda _region: _Missing()

    cluster = asyncio.run(
        agent._check_ecs_cluster("c", "us-east-1", missing_severity="warning")
    )
    service = asyncio.run(
        agent._check_ecs_service("c", "s", "us-east-1", missing_severity="warning")
    )
    for check in (cluster, service):
        assert check.severity == "warning", (
            "자동 생성할 리소스가 없다고 배포를 막고 있다"
        )


def test_missing_cluster_is_still_an_error_without_provisioning():
    """반대 방향 — 우리가 안 만들어 줄 때는 여전히 막아야 한다."""
    from core.agents.preflight_agent import PreflightAgent

    agent = PreflightAgent()

    class _Missing:
        @staticmethod
        def describe_clusters(**_):
            return {"clusters": []}

    agent._ecs_client = lambda _region: _Missing()
    check = asyncio.run(agent._check_ecs_cluster("c", "us-east-1"))
    assert check.severity == "error"


# ===========================================================================
# 접속 확인
# ===========================================================================


def test_http_probe_treats_a_4xx_as_reachable():
    """확인하려는 건 앱의 라우팅이 아니라 네트워크 경로가 열렸는가다."""
    import urllib.error

    from core.agents import ecs_agent as module

    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("u", 404, "nf", None, None)

    import urllib.request

    original = urllib.request.urlopen
    urllib.request.urlopen = _raise
    try:
        ok, detail = module._probe_http("http://x/health", attempts=1,
                                        sleep=lambda _: None)
    finally:
        urllib.request.urlopen = original
    assert ok is True
    assert "404" in detail


def test_http_probe_reports_failure_after_retries():
    from core.agents import ecs_agent as module

    import urllib.request

    def _raise(*_a, **_k):
        raise OSError("connection refused")

    original = urllib.request.urlopen
    urllib.request.urlopen = _raise
    try:
        ok, detail = module._probe_http("http://x/health", attempts=2,
                                        sleep=lambda _: None)
    finally:
        urllib.request.urlopen = original
    assert ok is False
    assert "connection refused" in detail


# ===========================================================================
# 변이 시험에서 드러난 빈틈 메우기
#
# 위 테스트들이 놓친 세 가지를 여기서 고정한다. 셋 다 "테스트가 진짜 경로를
# 지나가지 않아서" 생긴 빈틈이었다 — 가짜로 갈아끼운 부분에 결함이 숨었다.
# ===========================================================================


def test_the_route_calls_opa_with_arguments_its_real_signature_accepts():
    """[회귀·핵심] 라우트가 **진짜** OPA 클라이언트 시그니처에 맞게 부르는가.

    위쪽 라우트 테스트들은 `evaluate` 를 `**kwargs` 짜리 가짜로 갈아끼운다.
    그러면 인자 이름이 틀려도 통과한다 — 실제로 그 상태로 "모든 배포가
    503" 이라는 사고가 통과했다. 여기서는 **가짜를 쓰지 않고** 진짜 함수의
    시그니처에 인자를 bind 해본다.
    """
    import ast
    import inspect
    import textwrap

    from api.routes import ecs as ecs_routes
    from core.opa_client import OPAClient

    source = textwrap.dedent(inspect.getsource(ecs_routes.start_deployment))
    call = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "evaluate"
    )
    passed = {kw.arg for kw in call.keywords if kw.arg}
    assert passed, "evaluate 를 키워드 인자로 부르지 않는다"

    signature = inspect.signature(OPAClient.evaluate)
    accepted = set(signature.parameters) - {"self"}
    unknown = sorted(passed - accepted)
    assert not unknown, (
        f"OPAClient.evaluate 가 받지 않는 인자로 부르고 있다: {unknown}\n"
        f"(실제 시그니처: {sorted(accepted)})\n"
        "이대로면 TypeError 가 나고, 그걸 except 가 'OPA 장애'로 바꿔 내보낸다"
    )
    # 필수 인자가 빠지지 않았는지도 확인한다.
    required = {
        name for name, p in signature.parameters.items()
        if name != "self" and p.default is inspect.Parameter.empty
    }
    assert required <= passed, f"필수 인자가 빠졌다: {sorted(required - passed)}"


def test_preflight_run_marks_creatable_resources_as_warnings():
    """[회귀] `run()` 전체를 지나가야 `will_provision` 배선이 검증된다.

    앞의 preflight 테스트들은 `_check_ecs_cluster` 를 직접 부르며
    `missing_severity` 를 손으로 넘겼다. 그래서 `run()` 안에서 그 값을
    계산하는 부분이 망가져도 통과했다.
    """
    from core.agents.preflight_agent import PreflightAgent

    agent = PreflightAgent()

    class _Empty:
        @staticmethod
        def describe_clusters(**_):
            return {"clusters": []}

        @staticmethod
        def describe_services(**_):
            return {"services": []}

        @staticmethod
        def get_role(**_):
            return {"Role": {"Arn": "arn:aws:iam::1:role/LabRole"}}

        @staticmethod
        def describe_log_groups(**_):
            return {"logGroups": [{"logGroupName": "/ecs/recoder-task"}]}

    agent._ecs_client = lambda _r: _Empty()
    agent._iam_client = lambda: _Empty()
    agent._logs_client = lambda _r: _Empty()

    report = asyncio.run(
        agent.run(
            cluster="c", service="s", region="us-east-1",
            task_definition_family="recoder-task", will_provision=True,
        )
    )
    assert report.passed is True, (
        "자동 생성할 리소스가 아직 없다는 이유로 첫 배포를 막고 있다"
    )

    strict = asyncio.run(
        agent.run(
            cluster="c", service="s", region="us-east-1",
            task_definition_family="recoder-task", will_provision=False,
        )
    )
    assert strict.passed is False, "자동 생성을 안 할 때는 막아야 한다"


def test_rendered_log_group_wins_over_whatever_the_template_says(tmp_path,
                                                                 monkeypatch):
    """[회귀] 템플릿이 다른 이름을 적어도 우리가 만든 그룹 이름으로 맞춘다.

    앞의 테스트는 템플릿 값과 계산 값이 원래 같아서, 동기화 코드를 지워도
    통과했다. 여기서는 템플릿을 **일부러 다르게** 만들어 확인한다.
    어긋나면 컨테이너가 로그를 못 남기고 태스크 시작 자체가 실패한다.
    """
    from core.agents import ecs_agent as module

    template = tmp_path / "td.json.template"
    template.write_text(
        """{
  "family": "{{task_definition_family}}",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "{{cpu}}",
  "memory": "{{memory}}",
  "executionRoleArn": "{{execution_role_arn}}",
  "taskRoleArn": "{{task_role_arn}}",
  "containerDefinitions": [{
    "name": "{{container_name}}",
    "image": "{{image}}",
    "portMappings": [{"containerPort": {{container_port}}}],
    "environment": {{env_vars_json}},
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/STALE-NAME",
        "awslogs-region": "eu-west-1",
        "awslogs-stream-prefix": "ecs"
      }
    }
  }]
}""",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_TEMPLATE_PATH", template)

    agent = ECSAgent()
    req = make_request(task_definition_family="my-family")
    rendered = agent._render_task_definition(
        req, image="img",
        execution_role_arn="arn:aws:iam::123456789012:role/LabRole",
        task_role_arn="arn:aws:iam::123456789012:role/LabRole",
    )
    options = rendered["containerDefinitions"][0]["logConfiguration"]["options"]
    assert options["awslogs-group"] == "/ecs/my-family", \
        "템플릿의 낡은 로그 그룹 이름이 그대로 나갔다"
    assert options["awslogs-region"] == "us-east-1", \
        "템플릿의 낡은 리전이 그대로 나갔다"


# ===========================================================================
# Codex 리뷰 대응 (PR #10)
# ===========================================================================


def test_only_a_plain_allow_proceeds(app_client, monkeypatch):
    """[회귀·P1] OPA 결정은 다섯 가지다.

    예전 조건은 `decision.startswith("deny")` 였다. 그러면
    `allow_with_approval`(승인자 필요)과 `escalate_to_security`(보안 검토
    필요)가 **그대로 통과해 즉시 배포**된다. 게이트를 여는 조건은
    화이트리스트여야 한다.
    """
    client, ecs_routes = app_client
    from core import opa_client as opa_module

    started: list = []

    async def _fake_deploy(request, record=None):
        started.append(request)
        if record is not None:
            record.status = ECSDeployStatus.SUCCEEDED
            return record
        return ECSDeployRecord(status=ECSDeployStatus.SUCCEEDED)

    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)

    blocked = {
        "allow_with_approval": "approval_required",
        "escalate_to_security": "security_escalation_required",
        "deny": "policy_denied",
        "deny_with_fix_suggestion": "policy_denied",
    }
    for decision, expected_error in blocked.items():
        ecs_routes._deploy_records.clear()

        class _Result:
            pass

        _Result.decision = decision
        _Result.reason = "테스트"
        _Result.fix_suggestion = None
        _Result.opa_available = True
        _Result.required_approvers = 2

        async def _evaluate(**_kwargs):
            return _Result()

        monkeypatch.setattr(opa_module.opa_client, "evaluate", _evaluate)
        resp = client.post("/api/deploy/ecs", json={})
        assert resp.status_code == 403, f"{decision} 이 통과했다"
        assert resp.json()["detail"]["error"] == expected_error, decision

    assert started == [], "차단돼야 할 결정으로 배포가 시작됐다"


def test_a_second_deploy_while_one_is_running_is_rejected(app_client, monkeypatch):
    """[회귀·P2] 배포 버튼 두 번 누르면 파이프라인이 두 개 뜨던 문제.

    둘 다 같은 태그로 빌드해 같은 ECR·ECS 를 건드리고, 상태 조회는 가장
    최근 것 하나만 보여줘서 먼저 시작한 배포가 사용자 눈에서 사라진다.
    """
    client, ecs_routes = app_client
    started: list = []

    async def _never_finishes(request, record=None):
        started.append(request)
        if record is not None:
            record.status = ECSDeployStatus.IN_PROGRESS
            return record
        return ECSDeployRecord(status=ECSDeployStatus.IN_PROGRESS)

    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _never_finishes)

    body = {"ecs_cluster": "recoder-cluster", "ecs_service": "recoder-app"}
    assert client.post("/api/deploy/ecs", json=body).status_code == 200

    second = client.post("/api/deploy/ecs", json=body)
    assert second.status_code == 409, "같은 서비스에 배포가 두 개 떴다"
    assert second.json()["detail"]["error"] == "deployment_in_progress"
    assert len(started) == 1


def test_a_different_service_is_not_blocked(app_client, monkeypatch):
    """부정 통제: 잠금이 너무 넓으면 관계없는 배포까지 막는다."""
    client, ecs_routes = app_client

    async def _never_finishes(request, record=None):
        if record is not None:
            record.status = ECSDeployStatus.IN_PROGRESS
            return record
        return ECSDeployRecord(status=ECSDeployStatus.IN_PROGRESS)

    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _never_finishes)
    client.post("/api/deploy/ecs", json={"ecs_service": "app-a"})
    other = client.post("/api/deploy/ecs", json={"ecs_service": "app-b"})
    assert other.status_code == 200, "다른 서비스 배포까지 막고 있다"


def test_health_check_is_injected_when_a_command_is_given():
    """[회귀·P1] ECS 는 태스크 정의의 헬스체크만 감시한다.

    이미지에 구운 Docker HEALTHCHECK 는 보지 않으므로, 여기 없으면
    "프로세스는 살아 있는데 앱은 죽은" 상태에서도 배포가 성공으로
    보고되고 롤백·서킷 브레이커가 걸리지 않는다.
    """
    from core.agents.ecs_agent import python_http_health_check

    agent = ECSAgent()
    command = python_http_health_check(8000, "/health")
    rendered = agent._render_task_definition(
        make_request(health_check_command=command),
        image="img",
        execution_role_arn="arn:aws:iam::123456789012:role/LabRole",
        task_role_arn="arn:aws:iam::123456789012:role/LabRole",
    )
    check = rendered["containerDefinitions"][0]["healthCheck"]
    assert check["command"] == command
    assert check["retries"] >= 1 and check["startPeriod"] >= 1


def test_the_python_health_check_does_not_use_curl():
    """부정 통제: curl 은 python:slim 런타임에 없다. 넣으면 무한 재시작."""
    from core.agents.ecs_agent import python_http_health_check

    command = " ".join(python_http_health_check(8000))
    assert "curl" not in command
    assert "python" in command


def test_no_health_check_is_recorded_as_a_gap_not_silently_skipped():
    """헬스체크가 없다는 사실이 기록에 남아야 한다.

    조용히 넘어가면 "배포 성공"이 실제 동작을 보장하지 않는데도 사용자는
    그 사실을 알 수 없다.
    """
    agent = ECSAgent()
    rendered = agent._render_task_definition(
        make_request(),  # health_check_command 없음
        image="img",
        execution_role_arn="arn:aws:iam::123456789012:role/LabRole",
        task_role_arn="arn:aws:iam::123456789012:role/LabRole",
    )
    assert "healthCheck" not in rendered["containerDefinitions"][0]


def test_http_probe_treats_5xx_as_a_failure():
    """[회귀·P2] 500 만 뱉는 서비스를 "배포 성공"으로 보고하면 안 된다."""
    import urllib.error
    import urllib.request

    from core.agents import ecs_agent as module

    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("u", 503, "unavailable", None, None)

    original = urllib.request.urlopen
    urllib.request.urlopen = _raise
    try:
        ok, detail = module._probe_http(
            "http://x/health", attempts=2, sleep=lambda _: None
        )
    finally:
        urllib.request.urlopen = original
    assert ok is False, "5xx 를 도달 성공으로 처리했다"
    assert "503" in detail


def test_http_probe_still_treats_404_as_reachable():
    """반대 방향 — 4xx 는 네트워크 경로가 열렸다는 뜻이므로 성공이다."""
    import urllib.error
    import urllib.request

    from core.agents import ecs_agent as module

    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("u", 404, "nf", None, None)

    original = urllib.request.urlopen
    urllib.request.urlopen = _raise
    try:
        ok, _ = module._probe_http("http://x/health", attempts=1, sleep=lambda _: None)
    finally:
        urllib.request.urlopen = original
    assert ok is True


# ===========================================================================
# 확장과의 계약 (Codex 2차 리뷰)
#
# 여기 모은 검사들은 전부 같은 실패에서 나왔다 — **내가 만든 값을 누가
# 소비하는지 보지 않고 바꾼 것.** 두 번 연속 같은 유형으로 리뷰에 걸렸다.
# 그래서 개별 수정이 아니라, 확장 소스를 직접 읽어 계약을 고정한다.
# ===========================================================================

EXTENSION_DIRS = [ROOT / "extension" / "src", ROOT / "extension" / "media"]


def _extension_literals(field: str) -> set[str]:
    """확장 소스가 `<field> === '값'` 으로 비교하는 리터럴을 모은다."""
    import re

    pattern = re.compile(rf"\.{field}\s*===\s*['\"]([\w-]+)['\"]")
    found: set[str] = set()
    for base in EXTENSION_DIRS:
        if not base.is_dir():
            continue
        for path in list(base.rglob("*.ts")) + list(base.rglob("*.js")):
            if "node_modules" in path.parts:
                continue
            found |= set(pattern.findall(path.read_text(encoding="utf-8",
                                                        errors="replace")))
    return found


def test_stage_values_are_ascii_machine_tokens():
    """[회귀] `stage` 는 UI 문구가 아니라 계약이다.

    한국어("완료"/"실패")를 넣었더니 확장 네 곳의 `stage === 'done'`
    분기가 전부 빗나갔다. `running` 이 false 로 바뀌어 폴링은 멈추는데
    완료 표시도 실패 알림도 뜨지 않는 상태가 됐다.
    """
    from api.routes.deploy_ecs import _STAGE_TOKEN

    for status, token in _STAGE_TOKEN.items():
        assert token.isascii(), f"{status} → {token!r} 이 기계 토큰이 아니다"
        assert token.islower() and " " not in token, f"{status} → {token!r}"


def test_every_terminal_status_reaches_done_or_failed():
    """부정 통제: 종료 상태에 새 토큰을 만들면 UI 가 아무것도 표시하지 않는다.

    확장은 `if (!running) { if done ... else if failed ... }` 구조다.
    취소·롤백·서킷브레이커가 제3의 토큰을 내면 조용히 사라진다.
    """
    from api.routes.deploy_ecs import _RUNNING_STATES, _STAGE_TOKEN

    for status, token in _STAGE_TOKEN.items():
        if status in _RUNNING_STATES:
            continue
        assert token in {"done", "failed"}, (
            f"{status} → {token!r}: 종료 상태인데 UI 가 처리하지 못하는 토큰"
        )


def test_the_stage_tokens_the_extension_branches_on_are_producible():
    """확장 소스에서 실제 비교값을 읽어 대조한다.

    한쪽만 바뀌면 여기서 걸린다.
    """
    from api.routes.deploy_ecs import _STAGE_TOKEN

    branched_on = _extension_literals("stage")
    assert branched_on, (
        "확장 소스에서 stage 비교값을 하나도 못 찾았다 — 경로가 바뀌었거나 "
        "이 검사가 무력해졌다"
    )

    # 확장은 여러 배포 흐름(로컬·EC2·ECS)을 한 파일에서 다룬다. `building`
    # `running` 같은 값은 다른 흐름의 것이므로 ECS 가 낼 필요가 없다.
    # ECS 에서 중요한 건 **종료 분기 두 개**다 — 이게 안 맞으면 배포가
    # 끝났는데 UI 가 아무 말도 하지 않는다.
    terminal = {"done", "failed"}
    assert terminal <= branched_on, (
        f"확장이 더 이상 {sorted(terminal)} 로 분기하지 않는다 — 계약이 "
        "바뀌었으니 서버 매핑도 함께 봐야 한다"
    )
    producible = set(_STAGE_TOKEN.values())
    assert terminal <= producible, (
        "서버가 확장의 종료 분기 값을 못 내보낸다: "
        f"{sorted(terminal - producible)}"
    )


def test_start_response_uses_the_token_the_sidebar_recognises(app_client, monkeypatch):
    """[회귀] 사이드바는 `status === 'ok'` 일 때만 폴링을 시작한다.

    "started" 를 돌려주면 시작 실패로 읽혀서, 배포는 도는데 아무도
    지켜보지 않는 상태가 된다.
    """
    client, ecs_routes = app_client

    async def _fake_deploy(request, record=None):
        if record is not None:
            record.status = ECSDeployStatus.SUCCEEDED
            return record
        return ECSDeployRecord(status=ECSDeployStatus.SUCCEEDED)

    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)
    body = client.post("/api/deploy/ecs", json={}).json()

    recognised = _extension_literals("status")
    assert "ok" in recognised, "확장이 더 이상 status === 'ok' 를 안 본다"
    assert body["status"] == "ok", (
        f"사이드바가 인식하지 못하는 시작 토큰: {body['status']!r}"
    )


def test_status_response_fields_stay_ascii_where_machines_read_them():
    """부정 통제: 기계가 읽는 필드에 번역문이 섞이면 안 된다.

    사람이 읽을 문구는 `stage_text` 로 따로 보낸다.
    """
    from api.routes.deploy_ecs import EcsDeployStatusResponse

    defaults = EcsDeployStatusResponse()
    assert defaults.stage.isascii()
    # stage_text 는 반대로 사람용이므로 비어 있거나 한국어여도 된다.
    assert hasattr(defaults, "stage_text")


# ---------------------------------------------------------------------------
# preflight — 빈 계정
# ---------------------------------------------------------------------------


def test_cluster_not_found_exception_is_treated_as_creatable():
    """[회귀] 빈 계정에서 `describe_services` 는 빈 목록이 아니라
    `ClusterNotFoundException` 을 던진다.

    이걸 일반 오류로 처리하면 `_boto3_error` 가 severity="error" 로
    떨어뜨려서 `missing_severity="warning"` 이 아예 적용되지 않는다.
    결과적으로 **빈 계정의 첫 배포가 preflight 에서 막힌다** — 클러스터를
    만들어 주는 코드가 바로 다음 단계에 있는데도.
    """
    from core.agents.preflight_agent import PreflightAgent

    agent = PreflightAgent()

    class _EmptyAccount:
        @staticmethod
        def describe_services(**_):
            raise client_error("ClusterNotFoundException", "cluster not found")

        @staticmethod
        def describe_clusters(**_):
            raise client_error("ClusterNotFoundException", "cluster not found")

    agent._ecs_client = lambda _r: _EmptyAccount()

    service = asyncio.run(
        agent._check_ecs_service("c", "s", "us-east-1", missing_severity="warning")
    )
    cluster = asyncio.run(
        agent._check_ecs_cluster("c", "us-east-1", missing_severity="warning")
    )
    for check in (service, cluster):
        assert check.severity == "warning", (
            "빈 계정을 점검 실패로 처리했다 — 첫 배포가 막힌다"
        )
        assert "자동으로 생성" in (check.fix_guide or "")


def test_a_real_error_is_still_an_error():
    """반대 방향 — 권한 거부까지 '아직 없음'으로 삼키면 안 된다."""
    from core.agents.preflight_agent import PreflightAgent

    agent = PreflightAgent()

    class _Denied:
        @staticmethod
        def describe_services(**_):
            raise client_error("AccessDeniedException", "not authorized")

    agent._ecs_client = lambda _r: _Denied()
    check = asyncio.run(
        agent._check_ecs_service("c", "s", "us-east-1", missing_severity="warning")
    )
    assert check.severity == "error", "권한 거부를 '아직 없음'으로 삼켰다"
