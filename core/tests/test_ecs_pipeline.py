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


def init_git_repo(path, branch: str = "main"):
    """테스트용 git 저장소 하나를 만들고 `run(*args)` 를 돌려준다.

    git 이 없으면 **실패가 아니라 skip** 한다. 조용히 실패하게 두면
    "브랜치를 못 알아냄"이 되어, 테스트가 확인하려던 것과 정반대의 이유로
    통과해 버린다.
    """
    import os
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git 이 없어 브랜치 판정을 확인할 수 없다")

    path.mkdir(parents=True, exist_ok=True)

    # **개발자 개인 설정과 격리한다.** 전역 커밋 서명이나 전역 pre-commit
    # 훅이 걸려 있으면 아래 커밋이 실패하고, 그러면 이 테스트들이 "확인할 수
    # 없어서 skip" 이 아니라 **엉뚱한 이유로 실패**한다.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    })
    safe = ("-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull)

    def run(*args):
        head, rest = args[0], args[1:]
        argv = [head, *safe, *rest] if head == "git" else list(args)
        return subprocess.run(
            argv, cwd=path, capture_output=True, text=True, env=env
        )

    if run("git", "init", "-q", "-b", branch).returncode != 0:
        pytest.skip("git init -b 를 지원하지 않는 git 이다 (2.28 미만)")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (path / "f.txt").write_text("x", encoding="utf-8")
    run("git", "add", "-A")
    # 커밋이 실패하면 HEAD 가 unborn 이라 `rev-parse` 가 128 로 죽고 브랜치가
    # ""로 나온다. 그건 테스트가 확인하려던 것과 **정반대 이유**로 통과하는
    # 것이다. 여기서 멈춰야 한다.
    commit = run("git", "commit", "-qm", "init")
    assert commit.returncode == 0, (
        "테스트용 커밋이 실패했다 — 이대로면 브랜치 판정이 헛돈다:\n"
        f"{commit.stdout}{commit.stderr}"
    )
    head = run("git", "rev-parse", "--abbrev-ref", "HEAD")
    assert head.stdout.strip() == branch, (
        f"저장소가 기대한 브랜치에 있지 않다: {head.stdout.strip()!r} != {branch!r}"
    )
    return run


def client_error(code: str, message: str = "boom"):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}}, "Op")


# ===========================================================================
# 라우트 — 배포 버튼이 실제로 에이전트까지 닿는가
# ===========================================================================


@pytest.fixture
def app_client(monkeypatch, tmp_path):
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
    # 기록 저장소를 임시 경로로 돌린다. 안 그러면 개발자 홈의
    # ~/.recoder/ecs_deployments.json 을 테스트가 읽고 쓴다 — 실제 배포
    # 기록을 테스트가 덮어쓰는 건 물론이고, 남의 기록 때문에 결과가 달라진다.
    monkeypatch.setenv("RECODER_ECS_STORE", str(tmp_path / "ecs_deployments.json"))
    ecs_routes._deploy_records.clear()
    ecs_routes._service_operation_locks.clear()

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


def test_unreachable_opa_falls_back_to_the_local_rules(app_client, monkeypatch):
    """OPA 서버가 없으면 로컬 내장 규칙으로 판단한다.

    **계약이 바뀐 자리다.** 예전에는 여기서 무조건 503 이었는데, 리포
    어디에도 OPA 를 띄우는 것이 없어서 결과적으로 확장의 배포 버튼이
    한 번도 성공할 수 없었다. 정책을 건너뛰는 게 아니라, 이 경로를
    대체하기 전 구현이 쓰던 것과 같은 로컬 규칙을 적용한다.
    """
    client, ecs_routes = app_client
    from core import opa_client as opa_module

    class _Unavailable:
        decision = "deny"
        reason = "OPA 연결 실패"
        fix_suggestion = None
        opa_available = False

    async def _evaluate(**_kwargs):
        return _Unavailable()

    async def _fake_deploy(request, record=None):
        record.status = ECSDeployStatus.SUCCEEDED
        return record

    monkeypatch.setattr(opa_module.opa_client, "evaluate", _evaluate)
    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)

    resp = client.post("/api/deploy/ecs", json={})
    assert resp.status_code == 200, resp.text


def test_a_deny_that_survives_the_local_fallback_is_still_reported(
    app_client, monkeypatch
):
    """[부정 통제] 폴백이 막으면 그대로 막혀야 한다.

    폴백이 있다고 해서 OPA 부재가 자유 통과권이 되면 안 된다.
    """
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
    # SBOM 을 끄면 로컬 규칙 1번(SBOM 필수)에 걸린다.
    resp = client.post(
        "/api/ecs/deploy",
        json={"project_id": "p", "cluster": "c", "service": "s",
              "image": "i", "generate_sbom": False},
    )
    assert resp.status_code >= 400, "폴백이 게이트를 없애버렸다"
    assert resp.json()["detail"]["error"] in (
        "policy_denied", "opa_unavailable", "security_escalation_required"
    )


def test_a_broken_policy_client_is_not_reported_as_an_opa_outage(
    app_client, monkeypatch
):
    """부정 통제: 우리 쪽 결함을 외부 장애로 보고하면 원인을 못 찾는다."""
    client, ecs_routes = app_client
    from core import opa_client as opa_module

    async def _explode(**_kwargs):
        raise TypeError("evaluate() got an unexpected keyword argument 'policy_path'")

    monkeypatch.setattr(opa_module.opa_client, "evaluate", _explode)
    resp = client.post("/api/deploy/ecs", json={})
    assert resp.status_code == 500
    assert resp.json()["detail"]["error"] == "policy_evaluation_crashed"

    # 끝난 기록은 **한 벌로** 남아야 한다. 사유만 쓰고 끝난 시각·구제책을
    # 빼면 확장은 "끝났는데 언제 왜 끝났는지 모르는" 카드를 그린다.
    crashed = list(ecs_routes._deploy_records.values())[-1]
    assert crashed.status == ECSDeployStatus.FAILED
    assert crashed.completed_at is not None, "종료 시각이 비어 있다"
    assert crashed.error_remedy, "무엇을 하라는 말이 없다"
    # 그리고 재기동해도 남아 있어야 한다 — 저장을 안 하면 흔적이 사라진다.
    reloaded = ecs_routes._load_records().get(crashed.deployment_id)
    assert reloaded is not None, "정책 평가 크래시 기록이 디스크에 안 남았다"
    assert reloaded.status == ECSDeployStatus.FAILED
    assert "cost_warning" not in reloaded.provisioned, (
        "AWS 를 부른 적도 없는 배포에 요금 경고가 붙었다"
    )


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
        # 서비스 단위 runningCount 는 PRIMARY 배포의 것과 **다를 수 있다.**
        # 갱신 실패 중이면 PRIMARY 는 0 인데 이전 ACTIVE 배포의 태스크가
        # 살아 있다. 그 차이를 표현할 수 있어야 한다.
        service_running = frame.get(
            "serviceRunningCount", frame.get("runningCount", 0)
        )
        return {"services": [{
            "runningCount": service_running,
            "deployments": [dict(frame, status="PRIMARY")],
        }]}


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

    # 게이트 호출은 `_gate_and_start` 로 빠졌다 — 라우트만 보면 못 찾는다.
    source = textwrap.dedent(
        inspect.getsource(ecs_routes.start_deployment)
        + inspect.getsource(ecs_routes._gate_and_start)
    )
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


# ===========================================================================
# 보안 스캐너 결과 계약 — Codex 3차 #1
#
# `scan_all()` 은 `SecurityScanResult(image=..., dockerfile_path=...,
# repo_path=...)` 로 결과를 만든 뒤 `compute_pass()` 를 부르고, 호출부는
# `scan_passed` · `critical_count` 같은 이름으로 읽었다. 그런데 모델에는
# 그 중 **아무것도 없었다.** `passed` 는 required 라서 스캔은 도구가 한 번
# 돌기도 전에 ValidationError 로 죽었고, 확장에서 배포를 누르면 이미지를
# 빌드해 ECR 에 올린 다음 정상 배포가 전부 "실패"로 기록됐다.
#
# 아래 테스트들은 스캐너를 **가짜로 바꾸지 않는다.** 예전 테스트가 놓친
# 이유가 바로 `scan_all` 을 monkeypatch 해버렸기 때문이다 — 진짜 모델은
# 한 번도 구성되지 않았다.
# ===========================================================================


@pytest.fixture
def no_scanner_binaries(monkeypatch):
    """스캐너 바이너리가 하나도 없는 환경을 **강제**한다.

    이걸 환경에 맡기면 안 된다. trivy 가 깔린 개발 PC 에서는 스캔이 진짜로
    성공해 `tool_errors` 가 비고, CI 컨테이너에서는 비어 있지 않다 — 같은
    코드가 기계에 따라 다른 결과를 내는 테스트는 아무것도 지켜주지 못한다.
    (실제로 그렇게 써서 한 번 깨뜨렸다.)
    """
    from core.security_scan import SecurityScanner

    async def _missing(*_a, **_k):
        raise FileNotFoundError("binary not installed")

    monkeypatch.setattr(SecurityScanner, "_run_cmd", staticmethod(_missing))


def test_scan_all_can_actually_build_its_own_result():
    """[회귀] 스캐너가 결과 객체를 만드는 것부터 실패했다.

    `SecurityScanResult(image=..., dockerfile_path=..., repo_path=...)` 는
    모델에 `passed` 가 required 라서 ValidationError 로 죽었다. 도구가
    깔려 있든 아니든 **여기까지는 반드시 와야 한다.**
    """
    from core.schemas import SecurityScanResult
    from core.security_scan import security_scanner

    result = asyncio.run(
        security_scanner.scan_all(
            image="alpine:3.19", dockerfile_path=None, repo_path=None
        )
    )
    assert isinstance(result, SecurityScanResult)
    # 호출부가 읽는 이름들이 실제로 나와야 한다.
    for name in ("scan_passed", "critical_count", "hadolint_error_count",
                 "secret_count", "tool_errors"):
        assert hasattr(result, name), f"결과에 {name} 가 없다"


def test_scan_all_reports_the_tools_that_could_not_run(no_scanner_binaries):
    """도구가 없으면 "검사했는데 0건"으로 위장하지 않는다.

    그리고 **이미지 스캔(trivy)이 못 돌았으면 막는다.** 한 번도 들여다보지
    않은 이미지가 게이트를 통과하면 안 된다 — 검사 안 함은 위반 없음이
    아니다.
    """
    from core.security_scan import security_scanner

    result = asyncio.run(
        security_scanner.scan_all(
            image="alpine:3.19", dockerfile_path=None, repo_path=None
        )
    )
    assert "trivy_not_installed" in result.tool_errors
    assert not result.passed, "이미지를 스캔하지 못했는데 배포를 통과시켰다"


def _scan_result(*findings):
    from core.schemas import SecurityScanResult

    result = SecurityScanResult(image="img")
    result.findings = list(findings)
    result.compute_pass()
    return result


def _finding(tool: str, severity: str, title: str):
    from core.schemas import (
        SecurityFinding,
        SecurityScanSeverity,
        SecurityScanTool,
    )

    return SecurityFinding(
        tool=SecurityScanTool(tool),
        severity=SecurityScanSeverity(severity),
        title=title,
    )


def test_blocking_rules_match_the_design():
    """설계서 §Q3: critical·hadolint error·시크릿 → 차단. high → 경고."""
    assert _scan_result(_finding("trivy", "critical", "CVE-1")).blocked
    assert _scan_result(_finding("hadolint", "critical", "DL3008")).blocked
    assert _scan_result(_finding("gitleaks", "critical", "secret_leak:aws")).blocked

    warn_only = _scan_result(_finding("trivy", "high", "CVE-2"))
    assert warn_only.passed, "high 는 경고인데 배포를 막았다"
    assert warn_only.high_count == 1


def test_the_image_scanner_failing_to_run_blocks_the_deploy():
    """[보안] 이미지 취약점 검사(trivy)가 못 돌면 배포를 막는다.

    취약점이 0 건인 게 아니라 **검사를 못 한** 것이다. 배포 계약은 이미지
    스캔이 배포를 게이트하는 것이므로, 못 돈 경우 fail-closed 로 막는다.
    """
    result = _scan_result(_finding("trivy", "info", "trivy_not_installed"))
    assert not result.passed, "이미지를 스캔하지 못했는데 통과시켰다"
    assert result.critical_count == 0
    assert "trivy_not_installed" in result.tool_errors

    failed = _scan_result(_finding("trivy", "info", "trivy_scan_failed"))
    assert not failed.passed, "trivy 가 터졌는데 통과시켰다"


def test_the_gate_message_names_the_scanner_that_could_not_run():
    """차단당한 사용자가 **왜** 막혔는지 알아야 고친다."""
    result = _scan_result(_finding("trivy", "info", "trivy_not_installed"))
    blocked = ECSAgent._scan_gate_message(result)
    assert blocked is not None, "이미지 스캔 실패인데 게이트가 안 막았다"
    reason, fix = blocked
    assert "trivy_not_installed" in reason, reason
    assert "ecr:BatchGetImage" in fix or "설치" in fix, fix


def test_a_missing_source_scanner_does_not_block_the_deploy():
    """[부정 통제] 소스 검사(hadolint·gitleaks)는 자문이다 — 없다고 막지 않는다.

    개발 PC 에 그 도구가 없어도 배포는 되게 하고, 못 돌린 것은 경고로만
    표면화한다. 여기까지 막으면 도구 없는 PC 에서는 아무도 배포를 못 한다.
    이미지 스캔(trivy)이 못 돈 경우만 막는다(위 테스트).
    """
    result = _scan_result(
        _finding("hadolint", "info", "hadolint_not_installed"),
        _finding("gitleaks", "info", "gitleaks_not_installed"),
    )
    assert result.passed, "소스 검사 도구가 없다고 배포를 막았다"
    assert "hadolint_not_installed" in result.tool_errors
    assert "gitleaks_not_installed" in result.tool_errors


def test_the_counts_survive_serialisation():
    """집계값이 property 로만 있으면 레코드를 통째로 직렬화할 때 사라진다."""
    dumped = _scan_result(_finding("trivy", "critical", "CVE-1")).model_dump()
    for field in ("critical_count", "high_count", "hadolint_error_count",
                  "secret_count", "scan_passed"):
        assert field in dumped, f"직렬화에서 {field} 가 빠졌다"


def _attributes_read_from(
    source_path: Path, receiver: str, *, inside: str | None = None
) -> set[str]:
    """소스에서 `<receiver>.<attr>` 로 읽는 속성 이름을 긁어온다.

    `inside` 를 주면 그 이름의 함수 본문만 본다. 흔한 변수명(`result`)을
    파일 전체에서 찾으면 남의 지역변수까지 걸려들어, 검사가 엉뚱한 것을
    잡느라 정작 봐야 할 것을 못 본다.
    """
    import ast
    import re

    text = source_path.read_text(encoding="utf-8")
    if inside is not None:
        tree = ast.parse(text)
        bodies = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == inside
        ]
        assert bodies, f"{source_path.name} 에서 {inside}() 를 못 찾았다"
        text = "\n".join(ast.unparse(node) for node in bodies)
    return set(re.findall(rf"\b{re.escape(receiver)}\.([a-z_][a-z0-9_]*)", text))


def test_every_scan_field_the_deploy_path_reads_actually_exists():
    """[계약] 읽는 쪽과 모델을 대조한다.

    이번 사고의 본질은 "값을 바꾸면서 읽는 쪽을 확인하지 않은 것"이 아니라
    그 반대 — **읽는 코드를 쓰면서 모델을 확인하지 않은 것**이다. 사람이
    기억하는 대신 소스를 직접 읽어 대조한다.
    """
    from core.schemas import SecurityScanResult

    core_dir = Path(__file__).resolve().parents[1]
    readers = [
        (core_dir / "agents" / "ecs_agent.py", "record.scan_result", None),
        (core_dir / "security_scan.py", "result", "scan_all"),
    ]

    sample = SecurityScanResult()
    wanted: set[str] = set()
    for path, receiver, inside in readers:
        assert path.is_file(), f"경로가 바뀌었다: {path}"
        wanted |= _attributes_read_from(path, receiver, inside=inside)

    # 읽기 전용 속성만 검사한다 — 대입은 모델 필드가 아니어도 무방.
    assert {"scan_passed", "critical_count", "hadolint_error_count",
            "secret_count", "compute_pass"} <= wanted, (
        "대조할 속성을 못 찾았다 — 경로나 변수명이 바뀌어 이 검사가 "
        "무력해졌다"
    )
    missing = [name for name in wanted if not hasattr(sample, name)]
    assert not missing, f"읽는 코드는 있는데 모델에 없는 속성: {sorted(missing)}"


def test_a_blocking_scan_fails_the_deploy_with_readable_numbers():
    """게이트 메시지가 AttributeError 없이 만들어지는가."""
    record = ECSDeployRecord()
    record.scan_result = _scan_result(
        _finding("trivy", "critical", "CVE-1"),
        _finding("gitleaks", "critical", "secret_leak:aws"),
    )
    assert not record.scan_result.scan_passed
    message = (
        f"보안 스캔 실패: critical={record.scan_result.critical_count} "
        f"hadolint_err={record.scan_result.hadolint_error_count} "
        f"secrets={record.scan_result.secret_count}"
    )
    assert message == "보안 스캔 실패: critical=1 hadolint_err=0 secrets=1"


# ===========================================================================
# 서킷 브레이커가 **실제로** 배포를 멈추는가 — Codex 3차 #2
#
# 예전에는 브레이커가 "작동"해도 우리 기록의 상태값만 바뀌었다. AWS 에는
# 아무 말도 하지 않았으므로 ECS 는 죽는 태스크를 계속 새로 띄웠다.
# 기록에는 "자동 중단", 청구서에는 계속 과금.
# ===========================================================================


class RecordingEcs:
    """update_service / create_service 호출 인자를 그대로 보관한다."""

    def __init__(self):
        self.created: list[dict] = []
        self.updated: list[dict] = []

    def describe_services(self, **_):
        return {"services": []}

    def create_service(self, **kwargs):
        self.created.append(kwargs)
        return {"service": {"serviceArn": "arn:svc", "desiredCount":
                            kwargs.get("desiredCount", 1)}}

    def update_service(self, **kwargs):
        self.updated.append(kwargs)
        return {"service": {"serviceArn": "arn:svc", "desiredCount":
                            kwargs.get("desiredCount", 1)}}


def _breaker_config(kwargs: dict) -> dict:
    return kwargs.get("deploymentConfiguration", {}).get(
        "deploymentCircuitBreaker", {}
    )


def _assert_percentages_are_explicit(kwargs: dict) -> None:
    """UpdateService 는 이 구조체를 **통째로 교체**한다.

    일부만 보내면 나머지는 AWS 기본값으로 되돌아간다 — 누가 맞춰둔 값이
    배포할 때마다 조용히 원복된다. 그래서 두 퍼센트 값도 명시해야 한다.
    """
    config = kwargs.get("deploymentConfiguration", {})
    assert config.get("minimumHealthyPercent") is not None, (
        "minimumHealthyPercent 를 안 보냈다 — AWS 기본값으로 되돌아간다"
    )
    assert config.get("maximumPercent") is not None, (
        "maximumPercent 를 안 보냈다 — AWS 기본값으로 되돌아간다"
    )


def test_a_new_service_enables_the_ecs_circuit_breaker_without_auto_rollback():
    """[D17] 실패 반복은 멈추되, 이전 버전 복귀는 사람 승인을 기다린다."""
    ecs = RecordingEcs()
    aws_infra.ensure_service(
        ecs, cluster="c", service="s", task_definition="td:1",
        subnet_ids=["subnet-1"], security_group_ids=["sg-1"],
    )
    assert ecs.created, "서비스를 만들지 않았다"
    assert _breaker_config(ecs.created[0]) == {"enable": True, "rollback": False}
    _assert_percentages_are_explicit(ecs.created[0])


def test_an_updated_service_keeps_auto_rollback_disabled_too():
    """갱신 경로도 카드 승인 없이 이전 리비전으로 돌아가면 안 된다."""
    ecs = RecordingEcs()
    ecs.describe_services = lambda **_: {
        "services": [{"status": "ACTIVE", "serviceName": "s"}]
    }
    aws_infra.ensure_service(
        ecs, cluster="c", service="s", task_definition="td:2",
        subnet_ids=["subnet-1"], security_group_ids=["sg-1"],
    )
    assert ecs.updated, "서비스를 갱신하지 않았다"
    assert _breaker_config(ecs.updated[0]) == {"enable": True, "rollback": False}
    _assert_percentages_are_explicit(ecs.updated[0])


def test_the_breaker_can_be_turned_off_explicitly():
    ecs = RecordingEcs()
    aws_infra.ensure_service(
        ecs, cluster="c", service="s", task_definition="td:1",
        subnet_ids=["subnet-1"], security_group_ids=["sg-1"],
        circuit_breaker=False,
    )
    assert _breaker_config(ecs.created[0]) == {"enable": False, "rollback": False}


def _halt(record, ecs, **request_overrides):
    agent = ECSAgent()
    asyncio.run(
        agent._halt_failed_deployment(
            make_request(**request_overrides), record, {"ecs": ecs}
        )
    )


def test_a_failed_deploy_with_nothing_running_is_scaled_to_zero():
    """[회귀] 아무것도 안 떠 있는데 서비스를 놔두면 ECS 가 계속 재시도한다."""
    ecs = RecordingEcs()
    record = ECSDeployRecord()
    record.circuit_breaker_triggered = True
    record.running_task_count = 0
    record.service_created_by_this_run = True

    _halt(record, ecs)

    assert [u.get("desiredCount") for u in ecs.updated] == [0], (
        "실패했고 떠 있는 태스크도 없는데 태스크 수를 0 으로 내리지 않았다 — "
        "기록만 '자동 중단'이고 과금은 계속된다"
    )
    assert "0" in record.provisioned.get("halt", "")


def test_an_ecs_rollback_is_never_scaled_to_zero():
    """[부정 통제] ECS 가 되살린 이전 버전을 우리 손으로 끄면 안 된다."""
    ecs = RecordingEcs()
    record = ECSDeployRecord()
    record.circuit_breaker_triggered = True
    record.ecs_rolled_back = True
    record.running_task_count = 0   # 되돌리는 중이라 아직 0 일 수 있다

    _halt(record, ecs)

    assert ecs.updated == [], "ECS 가 복구한 서비스를 우리가 내렸다"


def test_a_slow_app_that_is_actually_running_is_not_killed():
    """뜨긴 떴는데 느린 앱을 자동으로 꺼버리면 멀쩡한 배포를 죽인다."""
    ecs = RecordingEcs()
    record = ECSDeployRecord()
    record.running_task_count = 1

    _halt(record, ecs)

    assert ecs.updated == []
    warning = record.provisioned.get("cost_warning", "")
    assert "stop" in warning, "끄는 방법을 알려주지 않았다"
    assert "헬스체크" in warning, (
        "헬스체크가 없어서 이 상태를 자동으로 못 잡는다는 사실을 안 알렸다"
    )


def test_the_halt_decision_does_not_rely_on_the_previous_revision():
    """[회귀] `previous_task_definition_arn` 은 "되돌아갈 곳이 있다"의 증거가 아니다.

    삭제됐다 다시 만들어진 서비스도, 똑같이 망가진 이전 리비전도 그 값을
    채운다. 예전 판정은 그걸 믿고 중지를 건너뛰었다 — 아무것도 안 떠 있는
    서비스를 "이전 버전으로 계속 동작합니다"라고 안내하면서.
    """
    ecs = RecordingEcs()
    record = ECSDeployRecord()
    record.circuit_breaker_triggered = True
    record.previous_task_definition_arn = "arn:aws:ecs:us-east-1:1:task-definition/app:7"
    record.running_task_count = 0   # 그 리비전은 지금 떠 있지 않다
    record.service_created_by_this_run = True

    _halt(record, ecs)

    assert [u.get("desiredCount") for u in ecs.updated] == [0], (
        "이전 리비전이 있다는 이유만으로 중지를 건너뛰었다"
    )


def test_failing_to_stop_the_service_does_not_hide_the_real_failure():
    """중지 시도가 터지면 원래 실패 원인이 그 예외에 가려진다."""
    class Broken(RecordingEcs):
        def update_service(self, **_):
            raise client_error("AccessDeniedException", "no ecs:UpdateService")

    message = aws_infra.halt_service(Broken(), cluster="c", service="s")
    assert "콘솔" in message, "직접 끄는 방법을 알려주지 않았다"


# ===========================================================================
# 서브넷만 지정했을 때 VPC 해석 — Codex 3차 #3
# ===========================================================================


def _vpc_with_subnets(ec2, cidrs, *, with_igw=False):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]
    vpc_id = vpc["VpcId"]
    ids = []
    for cidr in cidrs:
        subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr)["Subnet"]
        ids.append(subnet["SubnetId"])
    if with_igw:
        igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc_id)
        table = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
        ec2.create_route(RouteTableId=table, DestinationCidrBlock="0.0.0.0/0",
                         GatewayId=igw)
        for subnet_id in ids:
            ec2.associate_route_table(RouteTableId=table, SubnetId=subnet_id)
    return vpc_id, ids


def test_supplied_subnets_reveal_their_vpc():
    """[회귀] 예전에는 vpc_id 를 빈 문자열로 두고 넘어갔다."""
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        vpc_id, subnet_ids = _vpc_with_subnets(
            ec2, ["10.0.1.0/24", "10.0.2.0/24"], with_igw=True
        )
        target = aws_infra.resolve_subnet_network(ec2, subnet_ids)

    assert target.vpc_id == vpc_id
    assert set(target.subnet_ids) == set(subnet_ids)
    assert target.internet_routable is True


def test_user_chosen_subnets_are_warned_about_not_blocked():
    """사용자가 직접 찍어 준 서브넷은 막지 않는다.

    PrivateLink 엔드포인트로 ECR 에 닿는 사설 서브넷은 두 신호가 모두
    "인터넷 없음"인데도 멀쩡히 동작한다. 우리가 모르는 구성을 사용자가
    알고 있을 수 있으므로, 여기서 하드 실패시키면 정상 환경을 검증 로직으로
    깨뜨리게 된다 — 전에 한 번 한 실수다.
    """
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        _, subnet_ids = _vpc_with_subnets(ec2, ["10.0.1.0/24"], with_igw=False)
        for subnet_id in subnet_ids:
            ec2.modify_subnet_attribute(
                SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": False}
            )
        target = aws_infra.resolve_subnet_network(ec2, subnet_ids)

    assert set(target.subnet_ids) == set(subnet_ids), "사용자가 고른 서브넷을 버렸다"
    assert target.internet_routable is False, (
        "확인하지 못한 것을 확인했다고 보고하면 경고가 사라진다"
    )


def test_auto_discovery_still_refuses_when_it_cannot_reach_the_internet():
    """[부정 통제] 반대쪽은 여전히 막아야 한다.

    자동 탐색은 **우리가** 서브넷을 골랐다는 뜻이다. 우리가 고른 게 안 되는
    것이면 4분 뒤 CannotPullContainerError 를 보여주는 대신 지금 막는다.
    """
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        for subnet in ec2.describe_subnets()["Subnets"]:
            ec2.modify_subnet_attribute(
                SubnetId=subnet["SubnetId"], MapPublicIpOnLaunch={"Value": False}
            )
        with pytest.raises(aws_infra.NetworkNotFound) as caught:
            aws_infra.discover_default_network(ec2)

    assert "인터넷" in str(caught.value)


def test_more_subnets_than_ecs_accepts_are_refused_by_number():
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        _, subnet_ids = _vpc_with_subnets(
            ec2, [f"10.0.{n}.0/24" for n in range(1, 19)], with_igw=True
        )
        with pytest.raises(aws_infra.NetworkNotFound) as caught:
            aws_infra.resolve_subnet_network(ec2, subnet_ids)

    assert "16" in str(caught.value)


def test_user_chosen_subnets_are_not_silently_trimmed():
    """[회귀] 자동 탐색은 3개로 자르지만, 사용자가 다섯 개를 적었다면 다섯 개다.

    말없이 줄이면 AZ 를 넓게 쓰려던 의도가 조용히 무너진다.
    """
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        _, subnet_ids = _vpc_with_subnets(
            ec2, [f"10.0.{n}.0/24" for n in range(1, 6)], with_igw=True
        )
        target = aws_infra.resolve_subnet_network(ec2, subnet_ids)

    assert len(target.subnet_ids) == 5, (
        f"사용자가 5개를 지정했는데 {len(target.subnet_ids)}개만 남겼다"
    )


def test_a_private_subnet_is_dropped_instead_of_being_mixed_in():
    """[회귀] 공인·사설을 섞어 넘기면 태스크 배치에 따라 되기도 안 되기도 한다.

    예전에는 전부 그대로 넘기면서 `internet_routable=True` 라고까지
    보고했다 — 재현이 안 되는 최악의 실패 모양이다.
    """
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        vpc_id, public_ids = _vpc_with_subnets(ec2, ["10.0.1.0/24"], with_igw=True)
        private = ec2.create_subnet(
            VpcId=vpc_id, CidrBlock="10.0.9.0/24"
        )["Subnet"]["SubnetId"]
        target = aws_infra.resolve_subnet_network(ec2, public_ids + [private])

    assert private not in target.subnet_ids, (
        "인터넷으로 못 나가는 서브넷을 그대로 넘겼다"
    )
    assert set(target.subnet_ids) == set(public_ids)
    assert target.internet_routable is True


def test_the_public_ip_flag_is_accepted_as_a_weaker_signal():
    """라우팅에 igw- 가 안 보여도 공인 IP 자동 할당이 켜져 있으면 진행한다.

    다만 **확인했다고 말하지는 않는다** — 그 구분이 사라지면 경고가
    사라지고, 이미지 pull 실패의 첫 단서도 함께 사라진다.
    """
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        _, subnet_ids = _vpc_with_subnets(ec2, ["10.0.1.0/24"], with_igw=False)
        for subnet_id in subnet_ids:
            ec2.modify_subnet_attribute(
                SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True}
            )
        target = aws_infra.resolve_subnet_network(ec2, subnet_ids)

    assert set(target.subnet_ids) == set(subnet_ids)
    assert target.internet_routable is False, (
        "보조 신호로 진행한 것을 '확인됨'으로 보고했다"
    )


def test_subnets_from_two_vpcs_are_rejected():
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        _, first = _vpc_with_subnets(ec2, ["10.0.1.0/24"])
        second_vpc = ec2.create_vpc(CidrBlock="10.1.0.0/16")["Vpc"]["VpcId"]
        second = ec2.create_subnet(
            VpcId=second_vpc, CidrBlock="10.1.1.0/24"
        )["Subnet"]["SubnetId"]

        with pytest.raises(aws_infra.NetworkNotFound) as caught:
            aws_infra.resolve_subnet_network(ec2, first + [second])

    assert "다른 VPC" in str(caught.value)


def test_a_typo_in_a_subnet_id_is_reported_before_anything_is_created():
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        with pytest.raises(aws_infra.NetworkNotFound) as caught:
            aws_infra.resolve_subnet_network(ec2, ["subnet-deadbeef"])

    assert "subnet-deadbeef" in str(caught.value)


def test_custom_subnets_no_longer_require_a_paired_security_group():
    """[회귀] 요청 모델은 보안 그룹을 생략하면 자동 생성한다고 말한다.

    그런데 서브넷만 지정하면 VPC 를 몰라 그 자동 생성이 항상 실패했다 —
    문서에도 없는 "둘은 반드시 함께" 규칙이 조용히 생겨 있었다.
    """
    moto = pytest.importorskip("moto")
    import boto3

    agent = ECSAgent()
    with moto.mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        _, subnet_ids = _vpc_with_subnets(ec2, ["10.0.1.0/24"], with_igw=True)
        clients = {
            "ec2": ec2,
            "ecs": boto3.client("ecs", region_name="us-east-1"),
            "logs": boto3.client("logs", region_name="us-east-1"),
        }
        request = make_request(
            provision=True, subnet_ids=subnet_ids, security_group_ids=[]
        )
        record = ECSDeployRecord()
        target = asyncio.run(agent._step_provision(request, record, clients))

        groups = ec2.describe_security_groups(
            GroupIds=[record.provisioned["security_groups"]]
        )["SecurityGroups"]

    assert target.vpc_id, "VPC 를 해석하지 못했다"
    assert record.provisioned["vpc"] == target.vpc_id
    assert groups[0]["VpcId"] == target.vpc_id
    assert request.security_group_ids == [groups[0]["GroupId"]], (
        "만든 보안 그룹을 뒤 단계가 쓸 수 있게 요청에 되돌려 심지 않았다"
    )


# ===========================================================================
# ECS 자동 롤백을 성공으로 착각하지 않는가
#
# 서킷 브레이커에 rollback=True 를 켜면서 새로 생긴 위험이다. ECS 는 우리
# 리비전을 버리고 **이전 리비전을 다시 PRIMARY 로 올린다.** 그러면
# rolloutState=COMPLETED, running>=desired 가 되므로, 그것만 보던 폴러는
# "배포 성공"이라고 보고한다 — 사용자는 배포되지 않은 코드를 배포됐다고 믿고,
# URL 도 200 을 돌려준다(이전 버전이 응답하니까).
# ===========================================================================


OUR_ARN = "arn:aws:ecs:us-east-1:1:task-definition/app:8"
OLD_ARN = "arn:aws:ecs:us-east-1:1:task-definition/app:7"


def _poll(frames, monkeypatch, record=None):
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakePollEcs(frames))
    record = record if record is not None else ECSDeployRecord()
    result = asyncio.run(ECSAgent()._step_poll_deployment(make_request(), record))
    return result, record


def test_an_ecs_rollback_is_not_reported_as_a_successful_deploy(monkeypatch):
    """[회귀] 되돌아간 이전 버전이 응답한다고 우리 배포가 성공한 게 아니다."""
    record = ECSDeployRecord()
    record.task_definition_arn = OUR_ARN
    frames = [
        {"runningCount": 0, "desiredCount": 1, "failedTasks": 1,
         "rolloutState": "IN_PROGRESS", "taskDefinition": OUR_ARN},
        # ECS 가 이전 리비전을 되살렸다 — 겉보기에는 완벽한 성공이다.
        {"runningCount": 1, "desiredCount": 1, "failedTasks": 0,
         "rolloutState": "COMPLETED", "taskDefinition": OLD_ARN},
    ]
    (success, _f, breaker), record = _poll(frames, monkeypatch, record)

    assert success is False, "롤백된 배포를 성공이라고 보고했다"
    assert record.ecs_rolled_back is True
    assert "app:7" in record.provisioned.get("ecs_rollback", "")


def test_a_normal_deploy_of_our_own_revision_still_succeeds(monkeypatch):
    """[부정 통제] 태스크 정의를 대조한다고 정상 배포를 막으면 안 된다."""
    record = ECSDeployRecord()
    record.task_definition_arn = OUR_ARN
    frames = [
        {"runningCount": 1, "desiredCount": 1, "failedTasks": 0,
         "rolloutState": "COMPLETED", "taskDefinition": OUR_ARN},
    ]
    (success, _f, _b), record = _poll(frames, monkeypatch, record)

    assert success is True
    assert record.ecs_rolled_back is False


def test_a_deployment_ecs_itself_failed_counts_as_a_breaker_trip(monkeypatch):
    """AWS 의 판정을 우리 sliding window 보다 우선한다.

    우리 창은 15초 간격 표본이라 ECS 브레이커보다 늦게 반응하거나 아예
    임계값에 못 미친다. 그때 FAILED 를 평범한 실패로 처리하면 중지 로직이
    한 번도 돌지 않는다.
    """
    frames = [
        {"runningCount": 0, "desiredCount": 1, "failedTasks": 1,
         "rolloutState": "FAILED", "taskDefinition": OUR_ARN},
    ]
    (success, _f, breaker), _record = _poll(frames, monkeypatch)

    assert success is False
    assert breaker is True


def test_the_halt_decision_uses_the_service_wide_running_count(monkeypatch):
    """[회귀] PRIMARY 의 runningCount 는 **새 리비전만** 센다.

    멀쩡히 돌던 서비스를 갱신하다 실패하면 PRIMARY 는 0 인데 이전 ACTIVE
    배포의 태스크는 전부 살아 있다. 그걸 "떠 있는 게 없다"로 읽으면 중지
    로직이 **멀쩡한 이전 버전을 통째로 내린다.**
    """
    frames = [
        {"runningCount": 0, "serviceRunningCount": 2, "desiredCount": 1,
         "failedTasks": 0, "rolloutState": "IN_PROGRESS",
         "taskDefinition": OUR_ARN},
    ]
    _result, record = _poll(frames, monkeypatch)
    assert record.running_task_count == 2, (
        "PRIMARY 만 보고 있다 — 이전 버전의 살아 있는 태스크를 못 본다"
    )


def test_a_rolling_update_failure_never_takes_down_the_old_revision():
    """[회귀·P1] 갱신 실패로 새 리비전이 0 이어도 이전 버전은 건드리면 안 된다."""
    ecs = RecordingEcs()
    record = ECSDeployRecord()
    record.circuit_breaker_triggered = True
    record.running_task_count = 2          # 이전 버전이 아직 서비스 중
    record.service_created_by_this_run = False

    _halt(record, ecs)

    assert ecs.updated == [], (
        "갱신 실패인데 서비스 전체를 0 으로 내렸다 — 사용자가 요청하지 "
        "않은 장애다"
    )


def test_a_pre_existing_service_is_never_halted_even_with_nothing_running():
    """[부정 통제] 우리가 만들지 않은 서비스는 판단 대상이 아니다.

    떠 있는 게 없더라도 그건 사용자 쪽 사정일 수 있다.
    """
    ecs = RecordingEcs()
    record = ECSDeployRecord()
    record.circuit_breaker_triggered = True
    record.running_task_count = 0
    record.service_created_by_this_run = False

    _halt(record, ecs)

    assert ecs.updated == [], "원래 있던 서비스를 우리 판단으로 내렸다"
    assert "cost_warning" in record.provisioned


def test_cancellation_is_rechecked_after_the_url_step():
    """[회귀·P2] URL 확인은 최대 300초를 기다린다.

    그 사이 취소를 눌렀는데 다시 안 보면, 취소한 배포가 SUCCEEDED 로
    기록되고 새로 만든 서비스의 뒷정리도 건너뛴다.
    """
    import inspect

    source = inspect.getsource(ECSAgent._deploy_pipeline)
    after_url = source.split("_step_resolve_url(request, record, clients)", 1)[1]
    before_success = after_url.split("ECSDeployStatus.SUCCEEDED", 1)[0]
    assert "_abort_if_cancelled" in before_success, (
        "URL 확인 뒤 취소 확인 없이 곧바로 성공으로 표시한다"
    )


def test_a_missing_service_says_so_instead_of_crashing(monkeypatch):
    """[회귀] `resp.get("services", [{}])[0]` 는 IndexError 를 낸다.

    describe_services 는 없는 서비스에 대해 **키는 있고 목록은 빈** 응답을
    준다. 기본값 `[{}]` 는 절대 적용되지 않는다. 사용자에게는 "배포 중
    예상치 못한 오류"로만 보였다.
    """
    import boto3

    class Gone:
        def describe_services(self, **_):
            return {"services": [], "failures": [{"reason": "MISSING"}]}

    monkeypatch.setattr(boto3, "client", lambda *a, **k: Gone())
    with pytest.raises(aws_infra.InfraError) as caught:
        asyncio.run(ECSAgent()._step_poll_deployment(make_request(), ECSDeployRecord()))

    assert "MISSING" in (caught.value.detail or "")
    assert "삭제" in (caught.value.remedy or "")


def test_no_rollback_proposal_is_offered_for_something_ecs_already_undid(monkeypatch):
    """이미 되돌아간 일을 "승인하면 되돌립니다"라고 안내하면 안 된다.

    그 승인은 같은 리비전을 한 번 더 배포한다.
    """
    agent = ECSAgent()
    record = ECSDeployRecord()
    record.ecs_rolled_back = True
    record.previous_task_definition_arn = OLD_ARN

    called = []

    async def _spy(req, rec):
        called.append(rec)
        return "rollback-xxxx"

    monkeypatch.setattr(agent, "_create_rollback_proposal", _spy)

    async def _fake_poll(req, rec):
        return False, 3, True

    monkeypatch.setattr(agent, "_step_poll_deployment", _fake_poll)
    result = asyncio.run(
        _run_failure_branch(agent, record)
    )
    assert called == [], "ECS 가 이미 되돌린 배포에 롤백 제안을 또 만들었다"
    assert result.status == ECSDeployStatus.ROLLED_BACK
    assert "실행되고 있지 않습니다" in (result.error_message or "")


async def _run_failure_branch(agent, record):
    """`_deploy_pipeline` 의 실패 분기만 떼어 재현한다.

    파이프라인 전체를 돌리려면 AWS 가 필요하다. 여기서 보려는 것은
    "ECS 가 되돌렸을 때 어떤 기록이 남는가" 하나다.
    """
    request = make_request()
    record.health_check_failures = 3
    record.circuit_breaker_triggered = True
    record.status = ECSDeployStatus.CIRCUIT_BREAKER_TRIGGERED
    if record.ecs_rolled_back:
        record.status = ECSDeployStatus.ROLLED_BACK
        record.error_message = (
            "배포 실패 — ECS 가 이전 버전으로 자동 복구했습니다. "
            "이번에 올린 이미지는 실행되고 있지 않습니다."
        )
        return record
    record.rollback_proposal_id = await agent._create_rollback_proposal(request, record)
    return record


def test_the_failure_branch_helper_matches_the_real_pipeline():
    """[메타] 위 헬퍼가 실제 코드와 어긋나면 그 테스트는 아무것도 못 지킨다."""
    import inspect

    source = inspect.getsource(ECSAgent._deploy_pipeline)
    assert "if record.ecs_rolled_back:" in source, (
        "파이프라인에서 ECS 롤백 분기가 사라졌다"
    )
    assert source.index("if record.ecs_rolled_back:") < source.index(
        "record.rollback_proposal_id = await self._create_rollback_proposal"
    ), "롤백 제안 생성보다 뒤로 밀리면 제안이 먼저 만들어져 버린다"


# ===========================================================================
# 못 돌린 보안 검사를 사용자에게 알리는가
# ===========================================================================


def test_a_scan_that_never_ran_shows_up_in_the_record(no_scanner_binaries):
    """[회귀] tool_errors 를 계산만 하고 아무도 안 읽으면 없는 것과 같다.

    trivy 가 없거나 이미지를 못 받아오면 findings 가 비고, 그대로 두면
    "취약점 0건 = 안전"으로 읽힌다.
    """
    agent = ECSAgent()
    record = ECSDeployRecord()
    request = make_request(workspace_path=None)

    record = asyncio.run(
        agent._step_security_scan(request, record, "alpine:3.19")
    )

    assert record.scan_result is not None
    assert record.scan_result.tool_errors, "도구 부재가 결과에 안 남았다"
    warning = record.provisioned.get("scan_warning", "")
    assert "검사 결과가 아닙니다" in warning, (
        "검사를 못 돌렸다는 사실이 사용자에게 닿지 않는다"
    )


def test_every_scanner_leaves_a_trace_when_the_binary_is_missing(
    no_scanner_binaries,
):
    """[회귀] 조용히 실패한 스캐너는 "위반 0건"으로 둔갑한다.

    trivy 만 흔적을 남기고 hadolint·gitleaks 는 예외를 삼켰다. 그러면
    Dockerfile 을 한 번도 안 본 배포가 보안 게이트를 통과한다.
    """
    from core.schemas import _SCAN_NOT_PERFORMED_TITLES
    from core.security_scan import SecurityScanner

    scanner = SecurityScanner()
    cases = {
        "hadolint": lambda: scanner._run_hadolint("/tmp/Dockerfile"),
        "gitleaks": lambda: scanner._run_gitleaks("/tmp"),
        "trivy": lambda: scanner._run_trivy("img:v1"),
    }
    for tool, run in cases.items():
        titles = {f.title for f in asyncio.run(run())}
        assert titles & _SCAN_NOT_PERFORMED_TITLES, (
            f"{tool} 가 실행되지 못했는데 아무 흔적도 안 남겼다: {titles}"
        )


def test_a_scanner_that_crashes_mid_run_also_leaves_a_trace(monkeypatch):
    """도구는 깔려 있는데 **실행이 터진** 경우.

    위 테스트는 바이너리 부재(FileNotFoundError) 경로만 친다. 진짜 위험한
    건 이쪽이다 — 도구가 있으니 사용자는 검사가 돌았다고 믿는데, 예외가
    조용히 삼켜져 findings 가 비어 나온다.
    """
    from core.schemas import _SCAN_NOT_PERFORMED_TITLES
    from core.security_scan import SecurityScanner

    scanner = SecurityScanner()

    async def _boom(*_a, **_k):
        raise RuntimeError("hadolint died")

    monkeypatch.setattr(SecurityScanner, "_run_cmd", staticmethod(_boom))

    for tool, run in {
        "hadolint": lambda: scanner._run_hadolint("/tmp/Dockerfile"),
        "gitleaks": lambda: scanner._run_gitleaks("/tmp"),
        "trivy": lambda: scanner._run_trivy("img:v1"),
    }.items():
        titles = {f.title for f in asyncio.run(run())}
        assert titles & _SCAN_NOT_PERFORMED_TITLES, (
            f"{tool} 실행이 터졌는데 결과에는 아무 흔적도 없다: {titles}"
        )


def test_the_marker_titles_the_scanner_emits_are_the_ones_the_model_knows():
    """[계약] 스캐너가 쓰는 제목과 모델이 아는 제목이 어긋나면 조용히 새어나간다."""
    import re

    from core.schemas import _SCAN_NOT_PERFORMED_TITLES

    source = (Path(__file__).resolve().parents[1] / "security_scan.py").read_text(
        encoding="utf-8"
    )
    emitted = set(re.findall(r'title="((?:trivy|hadolint|gitleaks)_[a-z_]+)"', source))
    assert emitted, "스캐너에서 마커 제목을 하나도 못 찾았다 — 검사가 무력해졌다"
    assert emitted <= _SCAN_NOT_PERFORMED_TITLES, (
        "스캐너는 내보내는데 모델이 모르는 마커: "
        f"{sorted(emitted - _SCAN_NOT_PERFORMED_TITLES)}"
    )


# ===========================================================================
# 경고가 확장 화면까지 닿는가
#
# 확장은 log_tail 의 **끝 몇 줄만** 그린다. 그래서 "기록에 남겼다"와
# "사용자가 봤다"는 다른 얘기다. 실제로 scan_warning 은 기록에는 있었지만
# 항상 잘려 나가고 있었다.
# ===========================================================================


def _extension_tail_slice() -> int:
    """확장 소스에서 log_tail 을 몇 줄이나 그리는지 읽어온다."""
    import re

    root = Path(__file__).resolve().parents[2] / "extension"
    sizes = set()
    for path in list(root.rglob("*.ts")) + list(root.rglob("*.js")):
        if "node_modules" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"log_tail[^\n]{0,80}?slice\(\s*-(\d+)", text):
            sizes.add(int(match.group(1)))
        for match in re.finditer(r"\(s\.log_tail \|\| \[\]\)\.slice\(-(\d+)\)", text):
            sizes.add(int(match.group(1)))
    return min(sizes) if sizes else 3


def test_warnings_survive_the_slice_the_extension_applies():
    """[회귀] 경고는 log_tail 맨 뒤에 있어야 화면에 남는다."""
    from core.api.routes.deploy_ecs import to_status_response

    record = ECSDeployRecord()
    record.provisioned = {
        "cluster": "c",
        "log_group": "/ecs/app",
        "scan_warning": "실행되지 않은 보안 검사가 있습니다: trivy_not_installed",
        "subnets": "subnet-1",
        "security_groups": "sg-1",
        "service": "app (updated)",
    }
    record.image_uri = "1.dkr.ecr/app:1"
    record.service_url = "http://1.2.3.4:8000"

    tail = to_status_response(record).log_tail
    shown = tail[-_extension_tail_slice():]
    assert any("scan_warning" in line for line in shown), (
        "보안 검사를 못 돌렸다는 경고가 화면에 보이는 범위 밖으로 잘렸다: "
        f"{shown}"
    )
    assert "scan_warning" in tail[-1], (
        "마지막 한 줄만 보여주는 화면(WorkbenchPanel)에서도 보여야 한다"
    )


def test_plain_facts_still_come_before_the_warnings():
    """[부정 통제] 순서를 바꾼다고 정보가 사라지면 안 된다."""
    from core.api.routes.deploy_ecs import to_status_response

    record = ECSDeployRecord()
    record.provisioned = {"cluster": "c", "halt": "태스크 수를 0 으로 내렸습니다"}
    record.image_uri = "img"

    tail = to_status_response(record).log_tail
    assert [line.split(":")[0] for line in tail] == ["cluster", "image", "halt"]


# ===========================================================================
# 취소가 **실제로** 멈추는가 — P1
#
# 예전 취소는 기록의 status 만 FAILED 로 바꿨다. 파이프라인은 그걸 모른 채
# 계속 돌아 이미지를 올리고 서비스를 만든 뒤 같은 기록을 SUCCEEDED 로
# 덮어썼다. 사용자는 "취소됨"을 보고 손을 뗐는데 Fargate 태스크는 계속
# 과금됐다. 게다가 상태가 활성 목록에서 빠져 409 가드까지 뚫렸다.
# ===========================================================================


def test_cancel_actually_stops_the_pipeline():
    """[회귀] 취소 신호가 단계 경계에서 파이프라인을 끊어야 한다."""
    agent = ECSAgent()
    record = ECSDeployRecord()
    record.cancel_requested = True

    with pytest.raises(ECSAgent._Cancelled):
        agent._abort_if_cancelled(record, "빌드")


def test_a_pipeline_that_is_not_cancelled_runs_on():
    """[부정 통제] 취소를 안 눌렀으면 아무 일도 없어야 한다."""
    ECSAgent()._abort_if_cancelled(ECSDeployRecord(), "빌드")


def test_the_pipeline_checks_for_cancellation_before_it_costs_money():
    """[계약] 서비스를 만들기 **전에** 확인 지점이 있어야 한다.

    태스크가 뜬 뒤에야 확인하면, 취소를 눌러도 이미 과금이 시작돼 있다.
    """
    import inspect

    source = inspect.getsource(ECSAgent._deploy_pipeline)
    before_service = source.split("7. 서비스 확보")[0]

    # 개수만 세면 하나쯤 지워도 통과한다. **각 지점을 이름으로** 확인한다.
    for step in ("preflight", "인프라 확보", "빌드·업로드", "보안 스캔",
                 "태스크 정의 등록"):
        assert f'_abort_if_cancelled(record, "{step}")' in before_service, (
            f"'{step}' 뒤의 취소 확인 지점이 사라졌다 — 그 단계에서 취소하면 "
            "파이프라인이 계속 달려 다음 단계까지 진행한다"
        )
    # 폴링 루프 안에도 있어야 한다 — 없으면 최대 10분을 더 기다린다.
    assert "_abort_if_cancelled" in inspect.getsource(
        ECSAgent._step_poll_deployment
    ), "폴링 중에는 취소가 안 먹는다"


def test_cancel_only_marks_a_request_so_the_409_guard_still_holds(app_client):
    """[회귀] 취소가 상태를 바로 바꾸면 같은 서비스에 두 번째 배포가 들어온다.

    두 번째 파이프라인이 들어오면 첫 번째 폴러는 남의 리비전을 PRIMARY 로
    보고 "ECS 가 이전 버전으로 자동 복구했습니다"라는 **없는 사실**을
    보고한다. 게다가 그 분기는 태스크 중지를 일부러 건너뛴다.

    (배포를 실제로 돌리지 않고 진행 중 기록을 직접 넣는다. TestClient 는
     백그라운드 작업이 끝날 때까지 응답을 붙들고 있어서, 여기서 진짜
     파이프라인을 띄우면 테스트가 멈춘다.)
    """
    client, ecs_routes = app_client

    live = ECSDeployRecord(
        project_id="p", cluster="c", service="s",
        status=ECSDeployStatus.IN_PROGRESS,
    )
    ecs_routes._deploy_records[live.deployment_id] = live

    cancelled = client.post(f"/api/ecs/deploy/{live.deployment_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()

    assert body["cancel_requested"] is True, "취소 신호가 안 남았다"
    assert body["status"] == "in_progress", (
        f"파이프라인이 아직 안 멈췄는데 상태를 끝난 것으로 바꿨다: {body['status']}"
    )
    assert body["completed_at"] is None, (
        "멈추지도 않았는데 종료 시각을 찍었다"
    )

    second = client.post(
        "/api/ecs/deploy",
        json={"project_id": "p", "cluster": "c", "service": "s", "image": "i"},
    )
    assert second.status_code == 409, (
        "취소 요청 뒤 같은 서비스에 두 번째 파이프라인이 들어왔다"
    )


def test_cancelling_twice_is_harmless(app_client):
    client, ecs_routes = app_client
    live = ECSDeployRecord(project_id="p", cluster="c", service="s",
                           status=ECSDeployStatus.IN_PROGRESS)
    ecs_routes._deploy_records[live.deployment_id] = live

    first = client.post(f"/api/ecs/deploy/{live.deployment_id}/cancel")
    second = client.post(f"/api/ecs/deploy/{live.deployment_id}/cancel")
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["cancel_requested"] is True


def test_the_cancelled_pipeline_ends_as_cancelled_not_succeeded():
    """[회귀] 취소한 배포가 나중에 SUCCEEDED 로 덮어써지면 안 된다.

    예전에는 취소가 status 만 바꿨고 파이프라인은 끝까지 달려 같은 기록에
    SUCCEEDED 를 썼다 — 취소한 배포가 성공으로 남았다.
    """
    agent = ECSAgent()
    record = ECSDeployRecord(status=ECSDeployStatus.IN_PROGRESS)
    record.cancel_requested = True

    with pytest.raises(ECSAgent._Cancelled):
        agent._abort_if_cancelled(record, "빌드·업로드")

    # 파이프라인의 취소 분기가 하는 일을 그대로 확인한다.
    source = __import__("inspect").getsource(ECSAgent._deploy_pipeline)
    branch = source.split("except ECSAgent._Cancelled", 1)[1].split("except ")[0]
    assert "ECSDeployStatus.CANCELLED" in branch, (
        "취소로 끝났는데 CANCELLED 상태를 안 남긴다"
    )
    assert "_stop_after_cancel" in branch, "취소 뒤 뒷정리를 안 한다"


def test_cancel_is_honoured_before_the_zero_count_success_return():
    """[회귀] `desired_count=0` 갈래만 취소 재확인을 건너뛰었다.

    취소는 "지금 도는 단계가 끝난 뒤" 반영된다고 문서에 적어 놨다. 그런데
    태스크를 0 개로 두는 배포는 서비스 생성 직후 곧장 SUCCEEDED 를 쓰고
    끝나서, `_step_ensure_service` 가 AWS 를 기다리는 동안 사용자가 누른
    취소를 **없었던 일로 덮어썼다.** 다른 종료 지점(URL 확인 뒤)은 이미
    같은 이유로 다시 확인하고 있었는데 여기만 빠져 있었다.

    파이프라인 전체를 돌리지 않고 그 갈래만 본다 — 서비스 확보 직후
    취소가 걸려 있으면 성공으로 끝나면 안 된다.
    """
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(ECSAgent._deploy_pipeline))
    tree = ast.parse(source)

    zero = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "desired_count == 0" in ast.unparse(node.test)
    )
    calls = [
        node.func.attr for node in ast.walk(zero)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "_abort_if_cancelled" in calls, (
        "태스크 0 개 갈래가 취소를 다시 확인하지 않고 성공으로 끝낸다"
    )

    # 확인이 성공 기록보다 **먼저** 와야 한다. 뒤에 있으면 이미 덮어쓴 뒤다.
    abort_line = min(
        node.lineno for node in ast.walk(zero)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_abort_if_cancelled"
    )
    succeeded_line = min(
        node.lineno for node in ast.walk(zero)
        if isinstance(node, ast.Attribute) and node.attr == "SUCCEEDED"
    )
    assert abort_line < succeeded_line, (
        "취소 확인이 SUCCEEDED 기록보다 뒤에 있다 — 이미 덮어쓴 다음이다"
    )

    # 그리고 실제로 예외가 나야 한다.
    agent = ECSAgent()
    cancelled = ECSDeployRecord(status=ECSDeployStatus.IN_PROGRESS)
    cancelled.cancel_requested = True
    with pytest.raises(ECSAgent._Cancelled):
        agent._abort_if_cancelled(cancelled, "서비스 확보")
    assert cancelled.status != ECSDeployStatus.SUCCEEDED


@pytest.mark.parametrize("created_here", [True, False])
def test_a_cancelled_zero_count_deploy_reports_the_truth(created_here, monkeypatch):
    """[회귀] 태스크 0 개 배포를 취소했을 때 **뒷정리 문구가 거짓말이면 안 된다.**

    앞 테스트는 취소가 걸리는지만 본다. 취소가 걸리게 만들자 이번엔
    `_stop_after_cancel` 이 문제였다 — 그 함수는 `desired_count >= 1` 만
    오던 시절에 쓰였다.

      - 기존 서비스 갱신이었다면 "원래 앱을 내리지 않으려고 태스크는 그대로
        두었습니다"라고 `cost_warning` 에 쓴다. 그런데 `ensure_service` 가
        이미 `desiredCount=0` 으로 바꾼 뒤다 — **앱은 내려가 있는데 화면은
        그대로 두었다고 하고, 그것도 "요금이 나간다" 칸에 뜬다.**
      - 이번 실행이 만든 서비스였다면 `halt_service` 로 0 으로 내린다.
        이미 0 이다. 실패 경로에서 AWS 를 한 번 더 부르는데, 여기 온 원인이
        자격증명 문제라면 그 호출이 또 터진다.

    파이프라인을 실제로 돌려서 결과 문구를 본다 — 소스만 보면 이 결함이
    통째로 안 보인다.
    """
    agent = ECSAgent()
    halted: list[tuple[str, str]] = []

    async def _no_provision(self, req, rec, clients):
        return aws_infra.NetworkTarget(vpc_id="vpc-1", subnet_ids=("subnet-1",))

    async def _no_build(self, req, rec, clients):
        return "123456789012.dkr.ecr.us-east-1.amazonaws.com/recoder-app:v1"

    async def _register(self, req, clients, image):
        return "arn:aws:ecs:us-east-1:1:task-definition/recoder:7", None

    async def _ensure(self, req, rec, clients, task_def_arn, network):
        # 서비스 단계가 AWS 를 기다리는 동안 사용자가 취소를 눌렀다.
        rec.provisioned["service"] = "recoder-app (%s)" % (
            "created" if created_here else "updated"
        )
        rec.service_created_by_this_run = created_here
        rec.cancel_requested = True

    monkeypatch.setattr(ECSAgent, "_clients", staticmethod(lambda region: {}))
    monkeypatch.setattr(ECSAgent, "_step_provision", _no_provision)
    monkeypatch.setattr(ECSAgent, "_step_build_and_push", _no_build)
    monkeypatch.setattr(ECSAgent, "_step_register_task_definition", _register)
    monkeypatch.setattr(ECSAgent, "_step_ensure_service", _ensure)
    monkeypatch.setattr(
        aws_infra, "halt_service",
        lambda ecs, *, cluster, service: halted.append((cluster, service)) or "halted",
    )

    record = asyncio.run(agent._deploy_pipeline(
        make_request(desired_count=0, provision=True), ECSDeployRecord()
    ))

    assert record.status == ECSDeployStatus.CANCELLED, (
        f"취소를 눌렀는데 {record.status} 로 끝났다"
    )
    assert halted == [], (
        "태스크를 0 개로 요청한 배포인데 굳이 0 으로 내리는 AWS 호출을 했다"
    )
    assert "그대로 두었습니다" not in record.provisioned.get("cost_warning", ""), (
        "태스크가 이미 0 인데 '태스크는 그대로 두었습니다'라고 안내한다"
    )
    if not created_here:
        assert "내려가 있습니다" in record.provisioned.get("service_warning", ""), (
            "기존 앱을 0 으로 내려놓고 아무 말도 안 한다"
        )


def test_cancelling_an_update_rolls_the_service_back():
    """[회귀] 기존 서비스 갱신을 취소하면 **롤아웃을 되돌려야** 한다.

    `ensure_service` 가 이미 `UpdateService` 로 새(취소된) 태스크 정의로
    롤아웃을 시작했다. 태스크를 0 으로 안 내렸다고 안심하고 돌아가면, ECS 는
    계속해서 옛 태스크를 방금 취소한 버전으로 갈아치운다 — 기록은 CANCELLED
    인데 실제 배포는 끝까지 나간다. 이전 리비전으로 되돌려 그 롤아웃을 멈춘다.
    """
    agent = ECSAgent()

    updated = ECSDeployRecord()
    updated.provisioned["service"] = "app (updated)"
    updated.service_created_by_this_run = False
    updated.previous_task_definition_arn = "arn:aws:ecs:...:task-definition/app:6"

    ecs = RecordingEcs()
    asyncio.run(agent._stop_after_cancel(make_request(), updated, {"ecs": ecs}))

    # 이전 태스크 정의로 UpdateService 가 나가야 한다.
    assert any(
        u.get("taskDefinition") == "arn:aws:ecs:...:task-definition/app:6"
        for u in ecs.updated
    ), f"롤아웃을 되돌리지 않았다: {ecs.updated}"
    assert updated.provisioned.get("rollback"), "되돌렸다는 안내가 없다"
    # 태스크를 0 으로 내리는 halt 는 아니다 — 남의 앱이므로.
    assert all(u.get("desiredCount") != 0 for u in ecs.updated)


def test_cancelling_an_update_with_no_previous_revision_warns_clearly():
    """[부정 통제] 되돌릴 이전 리비전이 없으면 사용자가 직접 멈추게 안내한다."""
    agent = ECSAgent()
    rec = ECSDeployRecord()
    rec.provisioned["service"] = "app (updated)"
    rec.service_created_by_this_run = False
    rec.previous_task_definition_arn = None

    ecs = RecordingEcs()
    asyncio.run(agent._stop_after_cancel(make_request(), rec, {"ecs": ecs}))

    assert ecs.updated == [], "되돌릴 리비전이 없는데 UpdateService 를 불렀다"
    assert "배포 중지" in rec.provisioned.get("cost_warning", ""), (
        "되돌리지도 못하고 멈추는 법도 안 알려준다"
    )


def test_cancel_scales_down_only_a_service_this_run_created():
    """취소는 "이번 배포를 그만둔다"이지 "돌던 앱을 내린다"가 아니다."""
    agent = ECSAgent()

    created = ECSDeployRecord()
    created.provisioned["service"] = "app (created)"
    created.service_created_by_this_run = True
    ecs_created = RecordingEcs()
    asyncio.run(agent._stop_after_cancel(make_request(), created, {"ecs": ecs_created}))
    assert [u.get("desiredCount") for u in ecs_created.updated] == [0]

    updated = ECSDeployRecord()
    updated.provisioned["service"] = "app (updated)"
    updated.service_created_by_this_run = False
    ecs_updated = RecordingEcs()
    asyncio.run(agent._stop_after_cancel(make_request(), updated, {"ecs": ecs_updated}))
    assert ecs_updated.updated == [], (
        "사용자가 원래 돌리던 앱을 취소가 내려버렸다"
    )
    assert "cost_warning" in updated.provisioned


def test_cancelling_before_the_service_exists_touches_nothing():
    agent = ECSAgent()
    record = ECSDeployRecord()   # provisioned["service"] 없음
    ecs = RecordingEcs()
    asyncio.run(agent._stop_after_cancel(make_request(), record, {"ecs": ecs}))
    assert ecs.updated == []


# ===========================================================================
# 네트워크가 끊긴 것과 앱이 아픈 것 — P1
# ===========================================================================


def test_a_network_blip_does_not_trip_the_circuit_breaker(monkeypatch):
    """[회귀] wifi 가 45초 끊겼다고 멀쩡한 배포를 내리면 안 된다.

    예전에는 describe_services 실패를 그대로 실패 창에 넣었다. 3표본에
    50% 면 트립이므로, 연속 3회 실패(45초)로 서킷 브레이커가 걸리고
    `running_task_count == 0` 이라 서비스가 0 으로 내려갔다. 사용자에게는
    "배포 Health Check 실패"라고 표시됐다 — 앱은 멀쩡한데.

    자격증명 예외 목록만으로는 못 막았다. 연결 오류는 `.response` 가 없어
    `error_code()` 가 "" 를 돌려주므로 그 목록에 **절대** 안 걸린다.
    """
    import boto3
    from botocore.exceptions import EndpointConnectionError

    healthy = {"runningCount": 1, "desiredCount": 1, "failedTasks": 0,
               "rolloutState": "IN_PROGRESS", "taskDefinition": OUR_ARN}
    blip = EndpointConnectionError(endpoint_url="https://ecs.us-east-1.amazonaws.com")
    done = {"runningCount": 1, "desiredCount": 1, "failedTasks": 0,
            "rolloutState": "COMPLETED", "taskDefinition": OUR_ARN}

    frames = [healthy, healthy, blip, blip, blip, done]
    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakePollEcs(frames))

    record = ECSDeployRecord()
    record.task_definition_arn = OUR_ARN
    success, _failures, breaker = asyncio.run(
        ECSAgent()._step_poll_deployment(make_request(), record)
    )
    assert breaker is False, "네트워크 끊김이 서킷 브레이커를 걸었다"
    assert success is True, "연결이 돌아왔는데 배포를 실패로 처리했다"


def test_scattered_blips_never_accumulate_into_a_giveup(monkeypatch):
    """[회귀] 연속 카운터는 성공할 때마다 0 으로 돌아가야 한다.

    안 그러면 10분 폴링 동안 산발적으로 몇 번 끊긴 것만으로 배포를
    포기한다 — 그 사이 배포는 정상적으로 끝나가고 있는데.
    """
    import boto3
    from botocore.exceptions import EndpointConnectionError

    blip = EndpointConnectionError(endpoint_url="https://ecs.us-east-1.amazonaws.com")
    healthy = {"runningCount": 0, "desiredCount": 1, "failedTasks": 0,
               "rolloutState": "IN_PROGRESS", "taskDefinition": OUR_ARN}
    done = {"runningCount": 1, "desiredCount": 1, "failedTasks": 0,
            "rolloutState": "COMPLETED", "taskDefinition": OUR_ARN}

    # 총 6번 끊기지만 연속으로는 최대 1번.
    frames = [blip, healthy] * 6 + [done]
    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakePollEcs(frames))

    record = ECSDeployRecord()
    record.task_definition_arn = OUR_ARN
    success, _f, breaker = asyncio.run(
        ECSAgent()._step_poll_deployment(make_request(), record)
    )
    assert success is True, "산발적인 끊김이 쌓여서 배포를 포기했다"
    assert breaker is False


def test_a_sustained_outage_says_it_is_a_connection_problem(monkeypatch):
    """[부정 통제] 계속 못 부르면 포기하되, **앱 탓으로 돌리지 않는다.**"""
    import boto3
    from botocore.exceptions import EndpointConnectionError

    blip = EndpointConnectionError(endpoint_url="https://ecs.us-east-1.amazonaws.com")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakePollEcs([blip] * 20))

    with pytest.raises(aws_infra.InfraError) as caught:
        asyncio.run(ECSAgent()._step_poll_deployment(make_request(), ECSDeployRecord()))

    message = caught.value.message
    assert "연결" in message, f"연결 문제라고 말하지 않았다: {message}"
    assert "Health Check" not in message, "네트워크 문제를 앱 탓으로 돌렸다"
    assert "진행 중일 수 있습니다" in (caught.value.remedy or ""), (
        "배포가 살아 있을 수 있다는 사실을 안 알렸다"
    )


# ===========================================================================
# 실패로 끝났는데 태스크가 떠 있을 수 있는 경우 — P1
# ===========================================================================


def test_a_failure_after_the_service_exists_always_warns_about_cost():
    """[회귀] 자격증명 만료가 이 환경에서 가장 흔한 실패다.

    서비스는 이미 만들어져 태스크가 도는데 기록은 그냥 "실패"였다.
    사용자는 실패했다고 믿고 손을 떼고, Fargate 는 계속 과금한다.
    """
    record = ECSDeployRecord()
    record.provisioned["service"] = "app (created)"

    ECSAgent._warn_if_resources_may_be_running(make_request(), record)

    warning = record.provisioned.get("cost_warning", "")
    assert "stop" in warning, "끄는 방법을 안 알려줬다"


def test_no_cost_warning_before_any_service_was_touched():
    """[부정 통제] 아무것도 안 만들었으면 겁주지 않는다."""
    record = ECSDeployRecord()
    ECSAgent._warn_if_resources_may_be_running(make_request(), record)
    assert "cost_warning" not in record.provisioned


def test_the_warning_does_not_overwrite_an_actual_halt():
    record = ECSDeployRecord()
    record.provisioned["service"] = "app (created)"
    record.provisioned["halt"] = "태스크 수를 0 으로 내렸습니다"
    ECSAgent._warn_if_resources_may_be_running(make_request(), record)
    assert "cost_warning" not in record.provisioned


def test_every_terminal_failure_path_reports_cost(monkeypatch):
    """[계약] 실패 분기 세 곳 모두 비용 안내를 거쳐야 한다."""
    import inspect

    source = inspect.getsource(ECSAgent._deploy_pipeline)
    for branch in ("except InfraError", "except Exception"):
        after = source.split(branch, 1)[1].split("except ")[0]
        assert "_warn_if_resources_may_be_running" in after, (
            f"{branch} 분기에서 비용 안내가 빠졌다"
        )


# ===========================================================================
# Core 재시작 — P1
# ===========================================================================


def test_a_deployment_survives_a_core_restart(tmp_path, monkeypatch):
    """[회귀] 예전에는 기록이 메모리에만 있었다.

    Core 를 재시작하면(리포에 restart-core.bat 이 있을 만큼 흔하다) ECS
    서비스는 계속 돌며 과금되는데 상태 조회는 "idle" 을 돌려줬다. 화면에는
    배포된 게 없다고 뜨고, 멈출 방법도 없었다.
    """
    from api.routes import ecs as ecs_routes

    store = tmp_path / "ecs_deployments.json"
    monkeypatch.setenv("RECODER_ECS_STORE", str(store))
    monkeypatch.setattr(ecs_routes, "_deploy_records", {})

    live = ECSDeployRecord(
        cluster="my-cluster", service="my-app",
        status=ECSDeployStatus.IN_PROGRESS,
    )
    ecs_routes._deploy_records[live.deployment_id] = live
    ecs_routes._save_records()

    # ── 여기서 프로세스가 죽었다고 치고, 새로 읽는다 ──
    restored = ecs_routes._load_records()

    assert live.deployment_id in restored, "재시작 뒤 배포가 사라졌다"
    back = restored[live.deployment_id]
    assert back.cluster == "my-cluster" and back.service == "my-app", (
        "멈추려면 이름이 필요한데 그게 사라졌다"
    )
    assert "my-app" in back.provisioned.get("cost_warning", ""), (
        "태스크가 떠 있을 수 있다는 사실을 안 알렸다"
    )


def test_a_restart_does_not_lock_the_service_behind_409_forever(tmp_path, monkeypatch):
    """진행 중이던 기록을 그대로 되살리면 그 서비스는 영원히 잠긴다.

    그 파이프라인을 돌리던 프로세스는 죽었으므로 아무도 이어가지 않는데,
    409 가드는 계속 "배포가 돌고 있다"고 막는다.
    """
    from api.routes import ecs as ecs_routes

    store = tmp_path / "ecs_deployments.json"
    monkeypatch.setenv("RECODER_ECS_STORE", str(store))
    monkeypatch.setattr(ecs_routes, "_deploy_records", {})

    live = ECSDeployRecord(cluster="c", service="s",
                           status=ECSDeployStatus.IN_PROGRESS)
    ecs_routes._deploy_records[live.deployment_id] = live
    ecs_routes._save_records()

    monkeypatch.setattr(ecs_routes, "_deploy_records", ecs_routes._load_records())
    assert ecs_routes._active_deployment("c", "s") is None, (
        "재시작 뒤에도 배포가 '진행 중'으로 남아 그 서비스를 영구히 잠갔다"
    )


def test_a_corrupt_store_does_not_break_startup(tmp_path, monkeypatch):
    """[부정 통제] 기록 파일이 깨졌다고 Core 가 못 뜨면 안 된다."""
    from api.routes import ecs as ecs_routes

    store = tmp_path / "ecs_deployments.json"
    store.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv("RECODER_ECS_STORE", str(store))
    assert ecs_routes._load_records() == {}


def test_finished_deployments_are_restored_untouched(tmp_path, monkeypatch):
    """끝난 배포에까지 "결과를 알 수 없음"을 덧씌우면 안 된다."""
    from api.routes import ecs as ecs_routes

    store = tmp_path / "ecs_deployments.json"
    monkeypatch.setenv("RECODER_ECS_STORE", str(store))
    monkeypatch.setattr(ecs_routes, "_deploy_records", {})

    done = ECSDeployRecord(cluster="c", service="s",
                           status=ECSDeployStatus.SUCCEEDED,
                           service_url="http://1.2.3.4:8000")
    ecs_routes._deploy_records[done.deployment_id] = done
    ecs_routes._save_records()

    back = ecs_routes._load_records()[done.deployment_id]
    assert back.status == ECSDeployStatus.SUCCEEDED
    assert back.service_url == "http://1.2.3.4:8000"
    assert "cost_warning" not in back.provisioned


# ===========================================================================
# URL 접속을 확인 못 한 성공 — DoD 1번
# ===========================================================================


def test_an_unverified_url_is_visible_on_the_success_screen():
    """[회귀] 확장의 done 분기는 `error` 를 읽지 않고 "배포 완료 ✓" 만 그린다.

    카드 DoD 1번이 "URL 로 접속됨"인데, 접속을 확인하지 못한 사실이
    화면에서 사라지면 DoD 를 못 지켰는지도 모른 채 넘어간다.
    """
    from core.api.routes.deploy_ecs import _WARNING_KEYS, to_status_response

    assert "url_warning" in _WARNING_KEYS

    record = ECSDeployRecord(status=ECSDeployStatus.SUCCEEDED)
    record.provisioned = {
        "cluster": "c",
        "url_warning": "주소(http://1.2.3.4:8000)에 접속을 확인하지 못했습니다",
    }
    record.service_url = "http://1.2.3.4:8000"

    response = to_status_response(record)
    assert response.stage == "done"
    assert "url_warning" in response.log_tail[-1], (
        "성공 화면에서 '접속 확인 못 함'이 잘려 나갔다"
    )


def test_starting_a_deployment_writes_it_to_disk_before_running(app_client, monkeypatch):
    """[회귀] 파이프라인이 돌기 **전에** 기록이 디스크에 있어야 한다.

    프로세스가 배포 도중 죽으면 메모리 기록은 사라진다. 시작 시점에 남겨야
    재시작 뒤에도 클러스터·서비스 이름을 알고 태스크를 멈출 수 있다.
    """
    import json
    import os

    client, ecs_routes = app_client

    store = Path(os.environ["RECODER_ECS_STORE"])
    seen_at_start: list[list] = []

    async def _capture(request, record=None):
        # **파이프라인이 시작되는 순간** 디스크에 뭐가 있는지 본다.
        # 끝난 뒤에 확인하면 종료 시점의 저장 때문에 항상 통과해버려서,
        # 시작 전 저장이 사라져도 눈치채지 못한다.
        seen_at_start.append(
            json.loads(store.read_text(encoding="utf-8")) if store.is_file() else []
        )
        return record

    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _capture)

    resp = client.post(
        "/api/ecs/deploy",
        json={"project_id": "p", "cluster": "kluster", "service": "svc",
              "image": "i"},
    )
    assert resp.status_code == 202, resp.text
    assert seen_at_start, "배포 파이프라인이 시작되지 않았다"
    assert any(
        r["cluster"] == "kluster" and r["service"] == "svc"
        for r in seen_at_start[0]
    ), (
        "파이프라인이 시작될 때 디스크에 기록이 없었다 — 여기서 프로세스가 "
        "죽으면 돌고 있는 서비스의 이름조차 알 수 없어 멈출 방법이 없다"
    )


def test_the_module_restores_its_records_on_import(tmp_path, monkeypatch):
    """[회귀] 저장만 하고 안 읽으면 재시작 문제는 그대로다."""
    import importlib
    import json

    store = tmp_path / "ecs_deployments.json"
    orphan = ECSDeployRecord(
        cluster="c", service="s", status=ECSDeployStatus.IN_PROGRESS
    )
    store.write_text(
        json.dumps([orphan.model_dump(mode="json")], ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECODER_ECS_STORE", str(store))

    from api.routes import ecs as ecs_routes

    reloaded = importlib.reload(ecs_routes)
    try:
        assert orphan.deployment_id in reloaded._deploy_records, (
            "기동할 때 디스크의 배포 기록을 읽지 않는다 — 재시작하면 "
            "돌고 있는 서비스가 화면에서 사라진다"
        )
    finally:
        monkeypatch.delenv("RECODER_ECS_STORE", raising=False)
        importlib.reload(ecs_routes)


def test_an_unreachable_url_writes_the_warning_during_the_real_step(monkeypatch):
    """[회귀] 경고 문자열을 손으로 만든 테스트는 그 단계가 실제로 쓰는지 못 본다."""
    from core.agents import ecs_agent as agent_module

    monkeypatch.setattr(
        agent_module.aws_infra, "wait_for_public_url",
        lambda *a, **k: "http://1.2.3.4:8000",
    )
    monkeypatch.setattr(
        agent_module, "_probe_http",
        lambda *a, **k: (False, "URLError: timed out"),
    )

    record = ECSDeployRecord()
    request = make_request(url_wait_timeout=5)
    asyncio.run(
        ECSAgent()._step_resolve_url(request, record, {"ecs": None, "ec2": None})
    )

    assert "url_warning" in record.provisioned, (
        "접속 확인에 실패했는데 성공 화면에 보일 경고를 안 남겼다"
    )
    assert "1.2.3.4" in record.provisioned["url_warning"]


def test_a_reachable_url_leaves_no_warning(monkeypatch):
    """[부정 통제] 접속이 되면 경고를 만들면 안 된다."""
    from core.agents import ecs_agent as agent_module

    monkeypatch.setattr(
        agent_module.aws_infra, "wait_for_public_url",
        lambda *a, **k: "http://1.2.3.4:8000",
    )
    monkeypatch.setattr(agent_module, "_probe_http", lambda *a, **k: (True, "HTTP 200"))

    record = ECSDeployRecord()
    asyncio.run(
        ECSAgent()._step_resolve_url(
            make_request(url_wait_timeout=5), record, {"ecs": None, "ec2": None}
        )
    )
    assert "url_warning" not in record.provisioned
    assert record.service_url == "http://1.2.3.4:8000"


# ===========================================================================
# 스톡 설치에서 배포 버튼이 동작하는가 — P1
#
# 리포 어디에도 OPA 를 띄우는 것이 없다. 확장이 보내는 요청은
# approval_level 기본값이 3 이라 `_fail_closed` 가 항상 deny 를 돌려주고,
# 라우트는 503 을 낸다. 즉 스톡 상태에서 배포 버튼은 **한 번도 성공할 수
# 없었다.** 게다가 /ready 는 Docker 와 STS 만 보고 "준비됨"이라고 한다.
# ===========================================================================


@pytest.fixture
def app_client_without_opa(monkeypatch, tmp_path):
    """OPA 서버가 없는 스톡 환경. (기본 픽스처는 OPA 를 통과로 스텁한다)"""
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes import deploy_ecs, ecs as ecs_routes
    from core import opa_client as opa_module

    async def _unreachable(**kwargs):
        # 실제 오프라인 동작을 그대로 쓴다 — 여기서 결과를 손으로 만들면
        # fail-closed 규칙이 바뀌어도 테스트가 눈치채지 못한다.
        return opa_module.OPAClient._fail_closed(
            kwargs.get("level", 3), "OPA 서버에 연결할 수 없습니다"
        )

    monkeypatch.setattr(opa_module.opa_client, "evaluate", _unreachable)
    monkeypatch.setenv("RECODER_ECS_STORE", str(tmp_path / "s.json"))
    ecs_routes._deploy_records.clear()

    app = FastAPI()
    app.include_router(ecs_routes.router)
    app.include_router(deploy_ecs.router)
    return TestClient(app, raise_server_exceptions=False), ecs_routes


def test_the_deploy_button_works_without_an_opa_server(
    app_client_without_opa, monkeypatch
):
    """[회귀] OPA 가 없다고 배포 버튼이 항상 503 이면 제품이 아니다."""
    client, ecs_routes = app_client_without_opa

    async def _fake_deploy(request, record=None):
        if record is not None:
            record.status = ECSDeployStatus.SUCCEEDED
            return record
        return ECSDeployRecord(status=ECSDeployStatus.SUCCEEDED)

    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)

    resp = client.post("/api/deploy/ecs", json={})
    assert resp.status_code == 200, (
        f"OPA 서버가 없다고 배포가 막혔다: {resp.status_code} {resp.text}"
    )
    assert resp.json()["status"] == "ok"


def test_the_fallback_says_it_used_local_rules(app_client_without_opa, monkeypatch):
    """정책을 건너뛴 게 아니라 로컬 규칙으로 판단했다는 걸 알려야 한다."""
    client, ecs_routes = app_client_without_opa
    seen: list = []

    async def _fake_deploy(request, record=None):
        seen.append(record)
        record.status = ECSDeployStatus.SUCCEEDED
        return record

    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)
    client.post("/api/deploy/ecs", json={})

    assert seen and "policy_warning" in seen[0].provisioned, (
        "로컬 폴백을 썼다는 사실을 사용자에게 안 알렸다"
    )


def test_the_fallback_still_applies_the_preset_rules(app_client_without_opa, monkeypatch):
    """[부정 통제] 폴백은 무조건 통과가 아니다 — SBOM 없는 배포는 막는다."""
    client, ecs_routes = app_client_without_opa

    async def _fake_deploy(request, record=None):
        record.status = ECSDeployStatus.SUCCEEDED
        return record

    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)

    resp = client.post(
        "/api/ecs/deploy",
        json={"project_id": "p", "cluster": "c", "service": "s", "image": "i",
              "generate_sbom": False},
    )
    assert resp.status_code >= 400, (
        "폴백이 SBOM 없는 배포까지 통과시켰다 — 그건 게이트를 없앤 것이다"
    )


# ===========================================================================
# 보안 게이트가 무엇을 왜 막았는지 말하는가 — P1
# ===========================================================================


def test_the_scan_gate_names_secrets_and_where_they_are():
    """[회귀] 예전 메시지는 숫자만 나열하고 대처법은 의존성 얘기뿐이었다.

    실제로 가장 자주 걸리는 게 시크릿인데 한 마디도 없어서, 사용자는
    엉뚱한 곳을 고치게 됐다.
    """
    from core.schemas import (
        SecurityFinding, SecurityScanResult,
        SecurityScanSeverity as Sev, SecurityScanTool as Tool,
    )

    result = SecurityScanResult()
    result.findings = [
        SecurityFinding(tool=Tool.GITLEAKS, severity=Sev.CRITICAL,
                        title="secret_leak:aws_access_key_id",
                        location="tests/test_aws.py:1287"),
    ]
    result.compute_pass()

    message, remedy = ECSAgent._scan_gate_message(result)
    assert "시크릿" in message, f"무엇이 걸렸는지 안 말했다: {message}"
    assert "tests/test_aws.py:1287" in remedy, "어느 파일인지 안 알려줬다"


def test_a_passing_scan_produces_no_gate_message():
    from core.schemas import SecurityScanResult

    clean = SecurityScanResult()
    clean.compute_pass()
    assert ECSAgent._scan_gate_message(clean) is None
    assert ECSAgent._scan_gate_message(None) is None


def test_a_secret_in_the_workspace_blocks_before_anything_is_built(tmp_path, monkeypatch):
    """[회귀] 워크스페이스에 자격증명이 있으면 **빌드 전에** 막아야 한다.

    예전에는 스캔이 통째로 푸시 뒤에 있었다. 테스트용 AWS 키 하나만 있어도
    몇 분에 걸쳐 빌드하고 ECR 에 올린 다음에야 막혔다.

    소스를 읽는 게 아니라 파이프라인을 실제로 돌려 **빌드가 불렸는지**를
    본다. 문자열만 확인하면 그 호출을 `if False:` 로 감싸도 통과한다.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
    (workspace / "config.py").write_text(
        'AWS_KEY = "AKIA' + "Z" * 16 + '"\n', encoding="utf-8"
    )

    agent = ECSAgent()
    built: list[str] = []

    async def _no_build(self, req, rec, clients):
        built.append("built")
        return "img:1"

    async def _no_provision(self, req, rec, clients):
        return aws_infra.NetworkTarget(vpc_id="vpc-1", subnet_ids=("subnet-1",))

    monkeypatch.setattr(ECSAgent, "_clients", staticmethod(lambda region: {}))
    monkeypatch.setattr(ECSAgent, "_step_provision", _no_provision)
    monkeypatch.setattr(ECSAgent, "_step_build_and_push", _no_build)

    request = make_request(
        workspace_path=str(workspace), run_security_scan=True, provision=True
    )
    record = asyncio.run(agent._deploy_pipeline(request, ECSDeployRecord()))

    assert built == [], (
        "시크릿이 있는데도 이미지를 빌드했다 — 막힐 배포를 위해 ECR 에 "
        "이미지를 올리게 된다"
    )
    assert record.status == ECSDeployStatus.FAILED
    assert "시크릿" in (record.error_message or ""), record.error_message


def test_a_clean_workspace_is_not_blocked(tmp_path, monkeypatch):
    """[부정 통제] 깨끗한 워크스페이스까지 막으면 아무도 배포를 못 한다."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
    (workspace / "app.py").write_text("print('hello')\n", encoding="utf-8")

    agent = ECSAgent()
    built: list[str] = []

    async def _build(self, req, rec, clients):
        built.append("built")
        raise aws_infra.InfraError("여기까지만 확인한다")

    async def _no_provision(self, req, rec, clients):
        return aws_infra.NetworkTarget(vpc_id="vpc-1", subnet_ids=("subnet-1",))

    monkeypatch.setattr(ECSAgent, "_clients", staticmethod(lambda region: {}))
    monkeypatch.setattr(ECSAgent, "_step_provision", _no_provision)
    monkeypatch.setattr(ECSAgent, "_step_build_and_push", _build)

    request = make_request(
        workspace_path=str(workspace), run_security_scan=True, provision=True
    )
    asyncio.run(agent._deploy_pipeline(request, ECSDeployRecord()))
    assert built == ["built"], "깨끗한 워크스페이스인데 빌드까지 못 갔다"


def test_the_image_scan_keeps_the_findings_the_source_scan_already_made():
    """두 번에 나눠 돌리므로, 뒤 결과가 앞 결과를 덮어쓰면 안 된다."""
    from core.schemas import (
        SecurityFinding, SecurityScanResult,
        SecurityScanSeverity as Sev, SecurityScanTool as Tool,
    )
    from core.security_scan import security_scanner

    earlier = SecurityScanResult(repo_path="/ws")
    earlier.findings = [
        SecurityFinding(tool=Tool.GITLEAKS, severity=Sev.CRITICAL,
                        title="secret_leak:aws_access_key_id", location="a.py:1"),
    ]
    earlier.compute_pass()

    record = ECSDeployRecord()
    record.scan_result = earlier

    async def _image_only(image=None, dockerfile_path=None, repo_path=None):
        assert repo_path is None, "소스를 두 번 훑고 있다"
        assert dockerfile_path is None
        out = SecurityScanResult(image=image)
        out.compute_pass()
        return out

    original = security_scanner.scan_all
    security_scanner.scan_all = _image_only
    try:
        record = asyncio.run(
            ECSAgent()._step_security_scan(make_request(), record, "img:1")
        )
    finally:
        security_scanner.scan_all = original

    assert record.scan_result.secret_count == 1, (
        "이미지 스캔이 앞 단계에서 찾은 시크릿을 지워버렸다"
    )
    assert record.scan_result.blocked is True


# ===========================================================================
# 태스크 정의가 실계정에서 통하는가 — P1
# ===========================================================================


def test_the_log_driver_does_not_ask_the_execution_role_to_create_the_group():
    """[회귀] `awslogs-create-group: "true"` 는 실행 역할에 없는 권한을 요구한다.

    AWS 관리형 `AmazonECSTaskExecutionRolePolicy` 에는 logs:CreateLogGroup 이
    없다. 그러면 컨테이너가 기동 중에 죽고, 정작 로그 그룹은 비어 있어서
    "CloudWatch 로그를 보세요"라는 안내가 아무 도움이 안 된다.
    (우리 문서 docs/aws-minimum-permissions.md 에도 같은 경고가 있다.)

    로그 그룹은 우리가 `ensure_log_group` 으로 직접 만든다.
    """
    template = (
        Path(__file__).resolve().parents[1]
        / "registry" / "file_templates" / "ecs-task-definition.json.template"
    ).read_text(encoding="utf-8")
    assert "awslogs-create-group" not in template, (
        "실행 역할이 못 하는 일을 로그 드라이버에게 시키고 있다"
    )
    assert "awslogs-stream-prefix" in template, "스트림 접두어까지 지우면 안 된다"


def test_no_task_role_is_emitted_when_none_is_configured(monkeypatch):
    """[회귀] `ecsTaskRole` 은 AWS 가 만들어 주지 않는 역할이다.

    그런데 태스크 정의에 항상 들어갔다. 평범한 계정에서 기본 설정으로
    배포하면 없는 역할을 가리켜 PassRole 거부나 "unable to assume the role"
    로 죽는다. preflight 는 실행 역할만 보므로 잡지도 못한다.
    """
    from core import aws_policy

    monkeypatch.delenv(aws_policy.ENV_TASK_ROLE_ARN, raising=False)

    rendered = ECSAgent()._render_task_definition(
        make_request(),
        image="1.dkr.ecr.us-east-1.amazonaws.com/app:1",
        execution_role_arn="arn:aws:iam::111111111111:role/ecsTaskExecutionRole",
        task_role_arn="",
    )
    assert "taskRoleArn" not in rendered, (
        "지정하지도 않은 태스크 역할을 태스크 정의에 넣었다"
    )


def test_the_default_task_role_is_not_invented(monkeypatch):
    """[회귀] 환경변수가 없으면 태스크 역할 ARN 자체를 만들지 않는다.

    `resolve_roles()` 의 기본값 `ecsTaskRole` 은 AWS 가 만들어 주지 않는
    역할이다. ARN 을 만들어 넘기는 순간 태스크 정의에 들어가고, 없는 역할을
    가리켜 배포가 죽는다.
    """
    from core import aws_policy

    monkeypatch.delenv(aws_policy.ENV_TASK_ROLE_ARN, raising=False)

    class _Sts:
        @staticmethod
        def get_caller_identity():
            return {"Account": "111111111111"}

    exec_arn, task_arn = ECSAgent()._resolve_role_arns(
        make_request(), {"sts": _Sts()}
    )
    assert exec_arn.startswith("arn:aws:iam::111111111111:role/")
    assert task_arn == "", (
        f"지정하지도 않은 태스크 역할 ARN 을 만들어냈다: {task_arn}"
    )


def test_an_explicit_task_role_env_var_is_honoured(monkeypatch):
    """[부정 통제] 지정했으면 반드시 만들어야 한다."""
    from core import aws_policy

    monkeypatch.setenv(aws_policy.ENV_TASK_ROLE_ARN, "myTaskRole")

    class _Sts:
        @staticmethod
        def get_caller_identity():
            return {"Account": "111111111111"}

    _exec, task_arn = ECSAgent()._resolve_role_arns(make_request(), {"sts": _Sts()})
    assert task_arn.endswith("role/myTaskRole"), task_arn


def test_a_configured_task_role_is_still_used():
    """[부정 통제] 지정했으면 반드시 들어가야 한다."""
    rendered = ECSAgent()._render_task_definition(
        make_request(),
        image="1.dkr.ecr.us-east-1.amazonaws.com/app:1",
        execution_role_arn="arn:aws:iam::111111111111:role/exec",
        task_role_arn="arn:aws:iam::111111111111:role/myTaskRole",
    )
    assert rendered["taskRoleArn"] == "arn:aws:iam::111111111111:role/myTaskRole"


def test_a_bogus_task_role_arn_is_still_rejected():
    with pytest.raises(aws_infra.InfraError):
        ECSAgent()._render_task_definition(
            make_request(),
            image="img",
            execution_role_arn="arn:aws:iam::111111111111:role/exec",
            task_role_arn="arn:aws:iam::000000000000:role/ghost",
        )


# ===========================================================================
# SBOM 이 실제로 만들어지는가
# ===========================================================================


def test_the_sbom_record_model_accepts_what_the_generator_produces():
    """[회귀] generated_at 은 str 필드인데 datetime 을 넣고 있었다.

    pydantic ValidationError 가 나고 `except Exception` 이 그걸 삼켜서,
    **성공한 SBOM 이 매번 빈 결과로 둔갑**했다. 그런데도 기록에는
    sbom_version 이 찍혔다.
    """
    import inspect

    from core.schemas import SBOMRecord
    from core import sbom as sbom_module

    source = inspect.getsource(sbom_module.SBOMGenerator.generate)
    assert "generated_at=datetime.now(timezone.utc).isoformat()" in source, (
        "생성기가 여전히 datetime 을 넣고 있다"
    )
    assert "sbom_format=" not in source, "모델에 없는 필드를 넘기고 있다"

    # 모델이 실제로 그 값을 받는지도 확인한다.
    from datetime import datetime as _dt, timezone as _tz

    record = SBOMRecord(
        image="img", sbom_path="/tmp/s.json", package_count=2,
        generated_at=_dt.now(_tz.utc).isoformat(),
    )
    assert record.package_count == 2 and record.sbom_path


# ===========================================================================
# 정책 평가가 브랜치를 보는가 — Codex 4차 P1
#
# 확장이 `environment=production` 을 보내도, 변환 계층이 environment 만
# 컨테이너 환경변수로 옮기고 **branch 를 통째로 버렸다.** OPA 컨텍스트에도
# 로컬 폴백에도 branch 가 없어서, 프리셋 규칙 "프로덕션은 main 에서만"이
# 어느 경로로도 아무것도 막지 못했다. 규칙은 있는데 판단 재료가 없으면
# 그 규칙은 없는 것과 같다.
# ===========================================================================


def test_the_extension_request_carries_branch_and_environment():
    """[회귀] 변환 계층이 두 값을 버리면 정책이 눈을 감는다."""
    from core.api.routes.deploy_ecs import ExtensionEcsDeployRequest, to_core_request

    core_request = to_core_request(
        ExtensionEcsDeployRequest(environment="production", branch="feat/x")
    )
    assert core_request.environment == "production"
    assert core_request.branch == "feat/x"
    # 컨테이너 환경변수로도 계속 넘어가야 한다 — /version 이 이걸 읽는다.
    assert core_request.env_vars.get("ENVIRONMENT") == "production"


def test_both_policy_paths_read_the_same_context():
    """[계약] OPA 와 로컬 폴백이 **같은 함수**에서 사실을 받아야 한다.

    갈라 놓으면 한쪽만 값을 받는다. 실제로 그래서 이 사고가 났다.
    """
    import inspect

    from core.api.routes import ecs as ecs_routes

    context = ecs_routes._policy_context(
        make_request(environment="production", branch="feat/x")
    )
    assert context["branch"] == "feat/x"
    assert context["environment"] == "production"

    source = inspect.getsource(ecs_routes)
    assert "context=_policy_context(request)" in source, (
        "OPA 호출이 컨텍스트를 따로 조립하고 있다 — 폴백과 갈라진다"
    )
    assert source.count("_policy_context(") >= 3, (
        "폴백이 공통 컨텍스트를 안 쓰고 있다"
    )


def test_the_local_fallback_can_actually_reject_a_production_deploy():
    """[회귀] 브랜치가 비어 있으면 이 규칙은 절대 발동하지 않는다."""
    from core.opa_gate import OPAGate

    def judge(branch: str):
        return OPAGate()._local_deploy_gate({
            "sbom": {"present": True},
            "trivy": {"critical_count": 0, "high_count": 0},
            "gitleaks": {"passed": True},
            "hadolint": {"passed": True},
            "environment": "production",
            "branch": branch,
        })

    blocked = judge("feat/x")
    assert str(getattr(blocked.decision, "value", blocked.decision)) != "allow", (
        "기능 브랜치에서 프로덕션 배포가 통과했다"
    )
    allowed = judge("main")
    assert str(getattr(allowed.decision, "value", allowed.decision)) == "allow", (
        "main 에서의 프로덕션 배포까지 막았다"
    )


# ===========================================================================
# SBOM 을 못 만들면 배포를 세우는가 — Codex 4차 P1
# ===========================================================================


def test_a_failed_sbom_stops_the_deployment(monkeypatch):
    """[회귀] syft 가 없어도 배포가 계속되고 버전은 찍혔다.

    그러면 기록에는 **존재하지 않는 SBOM 의 버전**이 남고, 정책 게이트는
    `generate_sbom=True` 라는 요청만 보고 통과시킨 뒤 SBOM 없는 이미지가
    배포된다. 프리셋 정책 1번을 우리 손으로 우회한 셈이다.
    """
    from core.agents import ecs_agent as agent_module
    from core.schemas import SBOMRecord

    async def _empty(image):
        return SBOMRecord(image=image, sbom_path=None, error="syft not installed")

    monkeypatch.setattr(agent_module.sbom_generator, "generate", _empty)

    record = ECSDeployRecord()
    with pytest.raises(aws_infra.InfraError) as caught:
        asyncio.run(ECSAgent()._step_sbom(make_request(), record, "img:1"))

    assert "SBOM" in caught.value.message
    assert "syft" in (caught.value.remedy or ""), "설치 방법을 안 알려줬다"
    assert "generate_sbom" in (caught.value.remedy or ""), (
        "끄는 방법을 안 알려주면 사용자가 막힌 채로 끝난다"
    )
    assert not record.sbom_version, (
        "만들어지지도 않은 SBOM 의 버전을 기록에 남겼다"
    )


def test_a_successful_sbom_is_recorded(monkeypatch):
    """[부정 통제] 정상 SBOM 까지 막으면 안 된다."""
    from core.agents import ecs_agent as agent_module
    from core.schemas import SBOMRecord

    async def _ok(image):
        return SBOMRecord(image=image, sbom_path="/tmp/sbom.json", package_count=42)

    monkeypatch.setattr(agent_module.sbom_generator, "generate", _ok)

    record = asyncio.run(
        ECSAgent()._step_sbom(make_request(), ECSDeployRecord(), "img:1")
    )
    from datetime import datetime as _dt, timezone as _tz

    assert record.sbom_path == "/tmp/sbom.json"
    # 그냥 "v 로 시작"만 보면 아무 상수나 넣어도 통과한다.
    assert record.sbom_version == f"v{_dt.now(_tz.utc).strftime('%Y%m%d')}"
    assert record.sbom is not None and record.sbom.package_count == 42


# ===========================================================================
# provision=False 는 정말 아무것도 안 만드는가 — Codex 4차 P2
# ===========================================================================


def _no_provision_clients(*, cluster="", service="", log_group=""):
    """provision=False 검증에 쓸 AWS 환경을 원하는 만큼만 갖춰 준다."""
    import boto3

    ecs = boto3.client("ecs", region_name="us-east-1")
    logs = boto3.client("logs", region_name="us-east-1")
    if cluster:
        ecs.create_cluster(clusterName=cluster)
    if service:
        task_def = ecs.register_task_definition(
            family="probe", networkMode="awsvpc",
            requiresCompatibilities=["FARGATE"], cpu="256", memory="512",
            containerDefinitions=[{"name": "app", "image": "x", "essential": True,
                                   "memory": 512, "cpu": 256}],
        )["taskDefinition"]["taskDefinitionArn"]
        ecs.create_service(
            cluster=cluster, serviceName=service, taskDefinition=task_def,
            desiredCount=0, launchType="FARGATE",
        )
    if log_group:
        logs.create_log_group(logGroupName=log_group)
    return {"ecs": ecs, "logs": logs}


def _run_no_provision(clients, **overrides):
    request = make_request(
        provision=False, subnet_ids=["subnet-1"], security_group_ids=["sg-1"],
        **overrides,
    )
    record = ECSDeployRecord()
    target = asyncio.run(ECSAgent()._step_provision(request, record, clients))
    return target, record, request


def test_no_provision_requires_the_cluster():
    """[회귀] 클러스터를 안 보면 7단계에서 create_service 가 날 오류를 낸다."""
    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        clients = _no_provision_clients()
        with pytest.raises(aws_infra.InfraError) as caught:
            _run_no_provision(clients)
    # "클러스터"라는 낱말은 서비스 없음 메시지에도 들어간다. 두 메시지를
    # 확실히 가르는 것으로 본다 — 안 그러면 클러스터 검사를 지워도 통과한다.
    assert "create-cluster" in (caught.value.remedy or ""), (
        f"클러스터 부재를 짚지 못했다: {caught.value.message} / {caught.value.remedy}"
    )


def test_no_provision_requires_the_service_because_creating_one_costs_money():
    """[회귀] **가장 비싼 자리.**

    로그 그룹·리포지토리만 확인하던 때는 서비스 이름을 잘못 적어도 통과했다.
    그러면 7단계에서 **새 Fargate 서비스가 만들어지고 과금이 시작**된다.
    사용자의 관리 도구에는 안 보이는 이름이라 발견도 늦다.
    """
    moto = pytest.importorskip("moto")
    request = make_request()
    with moto.mock_aws():
        clients = _no_provision_clients(cluster=request.cluster)
        with pytest.raises(aws_infra.InfraError) as caught:
            _run_no_provision(clients)
    assert "서비스" in caught.value.message
    assert "요금" in (caught.value.remedy or ""), (
        "이름을 잘못 적으면 돈이 샌다는 걸 안 알려줬다"
    )


def test_no_provision_requires_the_log_group_up_front():
    """[회귀] 로그 그룹이 없으면 태스크가 기동 중에 죽는다.

    실행 역할에 `logs:CreateLogGroup` 이 없어서다. preflight 는 이걸
    경고로만 남겨 통과시킨다. 그리고 실패 안내는 "CloudWatch 로그를
    확인하세요"인데 **정작 로깅이 실패 원인이라 로그가 비어 있다.**
    """
    moto = pytest.importorskip("moto")
    request = make_request()
    with moto.mock_aws():
        clients = _no_provision_clients(
            cluster=request.cluster, service=request.service
        )
        with pytest.raises(aws_infra.InfraError) as caught:
            _run_no_provision(clients)
    assert "로그 그룹" in caught.value.message


def test_no_provision_passes_when_everything_already_exists():
    """[부정 통제] 다 갖춰 놨으면 통과해야 한다."""
    moto = pytest.importorskip("moto")
    request = make_request()
    with moto.mock_aws():
        clients = _no_provision_clients(
            cluster=request.cluster,
            service=request.service,
            log_group=f"/ecs/{request.task_definition_family}",
        )
        target, record, _ = _run_no_provision(clients)

    assert target.subnet_ids == ("subnet-1",)
    assert "기존" in record.provisioned.get("log_group", "")
    assert "기존" in record.provisioned.get("cluster", "")


def test_a_permission_error_is_not_reported_as_a_missing_resource():
    """[부정 통제] "없다"와 "볼 수 없다"는 대처법이 다르다."""
    class _Denied:
        @staticmethod
        def describe_repositories(**_):
            raise client_error("AccessDeniedException", "not authorized")

    with pytest.raises(aws_infra.InfraError) as caught:
        aws_infra.require_ecr_repository(_Denied(), "recoder-app")
    assert "권한" in (caught.value.remedy or ""), (
        "권한 문제인데 리포지토리를 만들라고 안내했다"
    )


def test_no_provision_does_not_create_the_ecr_repository():
    """[회귀] provision 을 꺼도 리포지토리는 만들고 있었다.

    "아무것도 만들지 않는다"는 약속이 반만 지켜진 상태였다.
    """
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        ecr = boto3.client("ecr", region_name="us-east-1")
        with pytest.raises(aws_infra.InfraError) as caught:
            aws_infra.require_ecr_repository(ecr, "recoder-app")
        assert "recoder-app" in caught.value.message

        ecr.create_repository(repositoryName="recoder-app")
        uri = aws_infra.require_ecr_repository(ecr, "recoder-app")

    assert uri.endswith("/recoder-app")


def test_the_build_step_only_creates_a_repository_when_provisioning():
    """[계약] 두 경로가 서로 다른 함수를 써야 한다."""
    import inspect

    source = inspect.getsource(ECSAgent._step_build_and_push)
    assert "require_ecr_repository" in source, (
        "provision=False 인데도 리포지토리를 만들고 있다"
    )
    assert "if req.provision:" in source


def test_the_route_fallback_rejects_production_from_a_feature_branch():
    """[회귀] 규칙 자체가 아니라 **라우트의 폴백 경로**를 관통해 확인한다.

    규칙만 직접 부르면 라우트가 branch 를 안 넘겨도 통과한다 —
    실제로 그 상태였다.
    """
    from core.api.routes import ecs as ecs_routes

    def judge(**over):
        return ecs_routes._local_policy_fallback(
            ECSDeployRecord(),
            make_request(environment="production", generate_sbom=True,
                         run_security_scan=True, **over),
        )

    blocked = judge(branch="feat/x")
    assert blocked is not None
    assert blocked.decision != "allow", (
        "기능 브랜치에서 프로덕션 배포가 폴백을 통과했다"
    )

    allowed = judge(branch="main")
    assert allowed is not None and allowed.decision == "allow", (
        "main 에서의 프로덕션 배포까지 막았다"
    )


def test_production_is_denied_when_the_branch_cannot_be_proven(tmp_path):
    """[보안 · 회귀] **모르는 브랜치는 통과시키지 않는다.**

    예전 규칙은 `branch and branch not in (...)` 이라 브랜치가 빈 문자열이면
    통째로 건너뛰었다. 그런데 브랜치를 모르는 경우 — 분리된 HEAD, git 이
    아닌 작업 폴더, 확장이 값을 안 보낸 경우 — 가 **가장 흔한 경우**다.
    즉 "프로덕션은 main 에서만"은 조문만 있고 한 번도 막은 적이 없었다.

    브랜치를 채우는 배관을 고치는 것만으로는 부족하다. 배관이 실패했을 때
    어디로 기우느냐가 실제 규칙이다.
    """
    from core.api.routes import ecs as ecs_routes
    from core.api.routes.deploy_ecs import ExtensionEcsDeployRequest, to_core_request

    # ① 규칙 자체 — 빈 브랜치 + production 이면 거부
    denied = ecs_routes._local_policy_fallback(
        ECSDeployRecord(),
        make_request(environment="production", branch="", generate_sbom=True,
                     run_security_scan=True),
    )
    assert denied is not None and denied.decision != "allow", (
        "브랜치를 모르는 프로덕션 배포가 통과했다"
    )
    assert "브랜치" in (denied.reason or "")

    # ② git 이 아닌 작업 폴더 → 요청의 branch 가 비고, 그 요청은 거부돼야 한다
    core_request = to_core_request(
        ExtensionEcsDeployRequest(workspace_path=str(tmp_path),
                                  environment="production")
    )
    assert core_request.branch == ""
    core_request.generate_sbom = True
    core_request.run_security_scan = True
    assert ecs_routes._local_policy_fallback(
        ECSDeployRecord(), core_request
    ).decision != "allow", (
        "git 이 아닌 폴더에서 프로덕션 배포가 그대로 나갔다"
    )

    # ③ 프로덕션이 아니면 브랜치를 몰라도 막지 않는다 (과잉 차단 방지)
    staging = ecs_routes._local_policy_fallback(
        ECSDeployRecord(),
        make_request(environment="staging", branch="", generate_sbom=True,
                     run_security_scan=True),
    )
    assert staging is not None and staging.decision == "allow", (
        "스테이징까지 막았다 — 이러면 아무도 못 쓴다"
    )


def test_a_detached_head_cannot_deploy_to_production(tmp_path):
    """[회귀] 분리된 HEAD 에서 `git rev-parse --abbrev-ref` 는 "HEAD" 를 준다.

    그걸 브랜치 이름으로 넘기면 규칙이 "현재: HEAD" 라는 엉뚱한 사유를 낸다.
    빈 값으로 정규화하고, 정규화된 빈 값이 **거부**로 이어지는지 본다 —
    정규화만 하고 거부로 이어지지 않으면 그게 바로 우회 경로다.
    """
    from core import branch_source
    from core.api.routes import ecs as ecs_routes

    repo = tmp_path / "ws"
    run = init_git_repo(repo, branch="main")
    head = run("git", "rev-parse", "HEAD").stdout.strip()
    run("git", "checkout", "-q", head)

    observed = branch_source.current_branch(str(repo))
    assert observed == "", f"분리된 HEAD 가 브랜치 이름인 척했다: {observed!r}"
    # 신고가 없으면 빈 값 그대로 — 그리고 빈 값은 프로덕션에서 거부다.
    assert branch_source.resolve_branch(str(repo), "") == ""

    result = ecs_routes._local_policy_fallback(
        ECSDeployRecord(),
        make_request(environment="production", branch="",
                     generate_sbom=True, run_security_scan=True),
    )
    assert result is not None and result.decision != "allow", (
        "분리된 HEAD 에서 프로덕션 배포가 통과했다"
    )


def test_the_fallback_will_not_pass_a_deployment_with_scans_turned_off():
    """[보안 · 회귀] 안 한 검사를 "통과"로 지어내지 않는다.

    폴백이 trivy/gitleaks/hadolint 를 "위반 없음"으로 넘기는 근거는 단 하나 —
    **파이프라인이 곧 실제로 검사하기 때문**이다. `run_security_scan=false`
    면 `_step_scan_sources` 와 `_step_security_scan` 이 통째로 건너뛰어져
    그 근거가 사라진다.

    그런데 OPA 는 리포 어디에도 띄우는 곳이 없어 **폴백이 기본 경로**다.
    즉 이 구멍은 예외 상황이 아니라, 기본 상태에서 승인 레벨 3 배포가
    보안 검사를 하나도 안 거치고 나가는 경로였다.
    """
    from core.api.routes import ecs as ecs_routes

    record = ECSDeployRecord()
    denied = ecs_routes._local_policy_fallback(
        record, make_request(run_security_scan=False, generate_sbom=True)
    )
    assert denied is not None, "폴백이 None 을 돌려주면 라우트가 503 으로 덮어쓴다"
    assert denied.decision != "allow", "스캔을 끈 배포가 폴백을 통과했다"
    assert denied.opa_available is True, (
        "opa_available=False 면 라우트가 이 거부 사유를 버리고 503 을 낸다"
    )
    assert "run_security_scan" in (denied.reason or ""), denied.reason
    assert record.provisioned.get("policy_warning"), "왜 막혔는지 기록이 없다"

    # 반대 방향 — 스캔을 켜면 통과해야 한다. 이게 없으면 "폴백은 항상 거부"로
    # 고쳐도 위 단언이 그대로 통과해 버린다.
    allowed = ecs_routes._local_policy_fallback(
        ECSDeployRecord(),
        make_request(run_security_scan=True, generate_sbom=True),
    )
    assert allowed is not None and allowed.decision == "allow", (
        "스캔을 켠 정상 배포까지 막았다"
    )


def test_the_duplicate_guard_cannot_be_split_by_an_await():
    """[동시성 · 회귀] 확인과 예약 사이에 `await` 가 들어가면 창이 열린다.

    확장 배포 버튼을 두 번 빠르게 누르면 두 요청이 같은 "진행 중 배포 없음"
    확인을 통과할 수 있다. 그러면 파이프라인이 둘 뜨고, 같은 태그로 빌드해
    같은 ECR 리포에 올리고 같은 ECS 서비스를 갱신한다. 게다가 상태 API 는
    최근 것 하나만 보여줘서 먼저 시작한 배포는 화면에서 사라진 채 계속 돈다.

    실제로 그 창이 생겼다 — 브랜치 판정을 executor 로 옮기면서 확인과 등록
    사이에 `await` 가 들어갔고, git 서브프로세스가 도는 동안 열려 있었다.

    **동기 함수 하나로 묶는 것이 원자성의 근거다.** 주석이 아니라 언어가
    막아야 나중에 누가 무엇을 추가해도 쪼개지지 않는다.
    """
    import ast
    import inspect
    import textwrap

    from core.api.routes import ecs as ecs_routes

    assert not inspect.iscoroutinefunction(ecs_routes._reserve_deployment), (
        "예약이 async 가 되면 그 안에 await 를 넣을 수 있게 된다 — "
        "확인과 등록이 쪼개지는 순간 중복 배포가 다시 가능해진다"
    )
    # 독스트링·주석이 아니라 **코드**에 await 가 없어야 한다.
    reserve_body = inspect.getsource(ecs_routes._reserve_deployment)
    assert not any(
        isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith))
        for node in ast.walk(ast.parse(textwrap.dedent(reserve_body)))
    ), "예약 구간에 await 가 생겼다 — 그 지점에서 확인과 등록이 쪼개진다"

    # 라우트는 **아무것도 await 하기 전에** 자리를 잡아야 한다.
    # 주석에 적힌 "await" 글자에 걸리지 않게 AST 로 본다.
    tree = ast.parse(textwrap.dedent(inspect.getsource(ecs_routes.start_deployment)))
    reserve_lines = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_reserve_deployment"
    ]
    await_lines = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Await)]
    assert reserve_lines, "라우트가 예약 함수를 부르지 않는다"
    assert not await_lines or min(reserve_lines) < min(await_lines), (
        "자리를 잡기 전에 await 하는 코드가 생겼다 — 그 틈으로 두 번째 "
        f"요청이 들어온다 (예약 {min(reserve_lines)}행, 첫 await {min(await_lines)}행)"
    )


def test_genuinely_concurrent_deploys_do_not_both_start(monkeypatch, tmp_path):
    """[동시성 · 회귀] **정말로 겹치게** 세 요청을 보낸다.

    `TestClient.post` 는 동기라서 요청 하나가 끝나야 다음이 나간다 — 즉
    이 결함이 사는 창(첫 요청이 브랜치 판정을 기다리는 동안)을 아예 만들지
    못한다. 그래서 순차 호출로 409 를 확인하는 테스트는 고치기 전 코드에서도
    그대로 통과했다. 진짜로 겹치려면 ASGI 전송 위에서 `asyncio.gather` 로
    보내고, 브랜치 판정이 **실제로 워커 스레드를 붙잡게** 해야 한다.

    고치기 전 모양(확인 → await → 삽입)에서는 셋 다 202 가 나고 기록이
    세 개 생긴다.
    """
    import time

    import httpx
    from fastapi import FastAPI

    from api.routes import deploy_ecs, ecs as ecs_routes
    from core import branch_source
    from core import opa_client as opa_module

    class _Allow:
        decision, reason, fix_suggestion = "allow", "", None
        opa_available, required_approvers = True, 0

    async def _evaluate(**_kwargs):
        return _Allow()

    async def _fake_deploy(request, record=None):
        # 이긴 요청의 기록이 나머지가 확인하는 동안 PENDING 으로 남아 있어야
        # 한다. 바로 끝내면 창이 닫혀 테스트가 헛돈다.
        await asyncio.sleep(0.2)
        return record

    def _slow_resolve(_workspace, claimed=""):
        # **동기 sleep** 이어야 한다. 라우트가 executor 로 넘기므로 진짜
        # 워커 스레드를 붙잡아야 코루틴이 await 지점에 머문다.
        time.sleep(0.3)
        return claimed or ""

    monkeypatch.setattr(opa_module.opa_client, "evaluate", _evaluate)
    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)
    monkeypatch.setattr(branch_source, "resolve_branch", _slow_resolve)
    monkeypatch.setenv("RECODER_ECS_STORE", str(tmp_path / "store.json"))
    monkeypatch.setattr(ecs_routes, "_deploy_records", {})

    app = FastAPI()
    app.include_router(ecs_routes.router)
    app.include_router(deploy_ecs.router)

    body = {
        "project_id": "p", "cluster": "recoder-cluster", "service": "recoder-app",
        "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/recoder-app:v1",
        "region": "us-east-1", "workspace_path": str(tmp_path),
    }

    async def _fire_three():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await asyncio.gather(*(
                c.post("/api/ecs/deploy", json=body) for _ in range(3)
            ))

    codes = sorted(r.status_code for r in asyncio.run(_fire_three()))
    assert codes == [202, 409, 409], (
        f"같은 클러스터/서비스에 배포가 여러 개 시작됐다: {codes}"
    )
    assert len(ecs_routes._deploy_records) == 1, (
        f"409 를 돌려주고도 기록을 남겼다: {list(ecs_routes._deploy_records)}"
    )


def test_a_failure_before_the_gate_releases_the_reservation(app_client, monkeypatch):
    """[회귀] 자리를 잡아 놓고 죽으면 그 서비스는 **영영 409** 가 된다.

    예약을 먼저 하도록 바꾼 대가다. PENDING 으로 남은 기록이 다음 배포를
    계속 막으므로, 게이트에 닿기 전에 터진 경우도 반드시 정리해야 한다.
    """
    from core import branch_source

    client, ecs_routes = app_client
    boom = {"count": 0}

    def _explode(*_args, **_kwargs):
        boom["count"] += 1
        raise RuntimeError("git 이 이상하다")

    monkeypatch.setattr(branch_source, "resolve_branch", _explode)

    body = {
        "project_id": "p", "cluster": "recoder-cluster", "service": "recoder-app",
        "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/recoder-app:v1",
        "region": "us-east-1",
    }
    first = client.post("/api/ecs/deploy", json=body)
    assert boom["count"] == 1
    assert first.status_code >= 500

    stuck = [r for r in ecs_routes._deploy_records.values()
             if r.status in ecs_routes._ACTIVE_STATUSES]
    assert not stuck, (
        f"예약이 PENDING 으로 남아 이 서비스는 영영 409 가 된다: {stuck}"
    )

    dead = list(ecs_routes._deploy_records.values())[-1]
    assert dead.error_remedy, "왜 못 시작했는지만 있고 어떻게 할지가 없다"

    # **디스크에도 반영돼야 한다.** 메모리에서만 FAILED 로 바꾸면, 다른
    # 요청이 흘린 `_save_records()` 가 이미 PENDING 으로 써 놓은 경우 그게
    # 남는다. 재기동하면 `_load_records` 가 그걸 "결과를 모르는 진행 중
    # 배포"로 보고 **있지도 않은 Fargate 태스크에 대한 요금 경고**를 만든다 —
    # AWS 를 한 번도 부른 적 없는 배포인데.
    reloaded = ecs_routes._load_records().get(dead.deployment_id)
    assert reloaded is not None, "실패 기록이 디스크에 아예 안 남았다"
    assert reloaded.status == ECSDeployStatus.FAILED, (
        f"디스크에는 아직 {reloaded.status} 로 남아 있다"
    )
    assert "cost_warning" not in reloaded.provisioned, (
        "AWS 를 부른 적도 없는 배포에 요금 경고가 붙었다: "
        f"{reloaded.provisioned.get('cost_warning')}"
    )

    # 그리고 다음 요청은 정상적으로 들어가야 한다.
    monkeypatch.setattr(branch_source, "resolve_branch", lambda *a, **k: "")

    async def _fake_deploy(request, record=None):
        return record

    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)
    assert client.post("/api/ecs/deploy", json=body).status_code == 202


def test_a_cancelled_request_does_not_leave_the_service_locked(monkeypatch, tmp_path):
    """[회귀] `except Exception` 은 **가장 그럴듯한 중단을 놓친다.**

    이 구간에서 제일 일어날 법한 중단은 `asyncio.CancelledError` 다 —
    우아한 종료(`timeout_graceful_shutdown`) 중에 요청이 executor 안의 git
    호출에서 기다리고 있으면 그렇게 끊긴다. 그런데 그건 `Exception` 이
    아니라 `BaseException` 이라, `except Exception` 으로는 안 걸린다.
    잡아 놓은 자리가 PENDING 으로 남아 그 서비스는 영영 409 가 된다 —
    핸들러가 막겠다고 적어 놓은 바로 그 상황이다.
    """
    from fastapi import BackgroundTasks

    from api.routes import ecs as ecs_routes
    from core import branch_source

    def _cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(branch_source, "resolve_branch", _cancelled)
    monkeypatch.setenv("RECODER_ECS_STORE", str(tmp_path / "store.json"))
    monkeypatch.setattr(ecs_routes, "_deploy_records", {})

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ecs_routes.start_deployment(
            make_request(), BackgroundTasks(), None
        ))

    stuck = [r for r in ecs_routes._deploy_records.values()
             if r.status in ecs_routes._ACTIVE_STATUSES]
    assert not stuck, (
        f"요청이 취소됐는데 예약이 PENDING 으로 남았다 — 이 서비스는 "
        f"영영 409 가 된다: {stuck}"
    )


def test_a_low_approval_level_cannot_skip_the_policy_gate():
    """[보안 · 회귀] 게이트를 통과하는 조건을 **호출자가 고를 수 있으면 안 된다.**

    `OPAClient._fail_closed` 는 OPA 가 없을 때 레벨 1~2 를
    `decision="allow", opa_available=False` 로 통과시킨다. 라우트는 예전에
    `decision != "allow"` 일 때만 로컬 폴백을 불렀으므로, 요청 본문에
    `approval_level: 1` 한 줄만 넣으면 **SBOM·스캔·브랜치 규칙이 전부
    호출조차 되지 않았다.** 스캔을 끈 배포를 막는 규칙을 아무리 정교하게
    만들어도 그 앞에서 통째로 우회됐다.

    두 가지를 함께 확인한다 — 레벨을 못 내리는 것과, 레벨과 무관하게
    OPA 가 없으면 로컬 규칙이 판단하는 것.
    """
    from core.api.routes import ecs as ecs_routes

    # 바닥은 **구체적인 값**이어야 한다. 상수와 비교만 하면 상수를 1 로
    # 낮춰도 테스트가 그대로 통과한다 — 지키려던 것이 사라진다.
    assert ecs_routes.ECS_DEPLOY_MIN_APPROVAL_LEVEL >= 3, (
        "ECS 배포는 컨테이너를 띄우고 과금을 발생시키는 Level 3 작업이다. "
        "바닥이 2 이하면 OPA 가 없을 때 fail-closed 가 통과시킨다"
    )
    assert ecs_routes._effective_approval_level(make_request(approval_level=1)) == 3
    assert ecs_routes._effective_approval_level(make_request(approval_level=4)) == 4, \
        "더 엄격하게 받겠다는 요청까지 깎으면 안 된다"


def test_a_low_approval_level_cannot_skip_the_policy_gate_through_the_route(
    app_client, monkeypatch
):
    """[보안 · 회귀] 위 단위 확인을 **라우트를 관통해** 다시 본다.

    함수만 확인하면 라우트가 그 함수를 안 부르도록 바뀌어도 통과한다.
    실제로 그 형태였다 — 레벨을 낮추면 `_fail_closed` 가 allow 를 주고,
    라우트는 allow 면 로컬 폴백을 아예 안 불렀다.
    """
    from core import opa_client as opa_module

    client, ecs_routes = app_client

    class _OfflineAllow:
        """OPA 가 없는데 `allow` 를 돌려주는 경우.

        `OPAClient._fail_closed` 가 레벨 1~2 에서 실제로 이렇게 답한다.
        여기서는 **레벨과 무관하게** 이 답을 강제한다 — 라우트가 결정값이
        아니라 `opa_available` 로 폴백을 부르는지 그 자체를 보기 위해서다.
        레벨 바닥(`_effective_approval_level`)이 있어서 실제로는 이 답이
        안 나오지만, 두 방어선 중 하나만 살아 있어도 통과하는 테스트는
        나머지 하나가 조용히 사라지는 것을 못 잡는다.
        """
        decision = "allow"
        reason = "OPA 오프라인"
        fix_suggestion = None
        opa_available = False
        required_approvers = 0

    seen_levels: list[int] = []

    async def _evaluate(**kwargs):
        seen_levels.append(kwargs["level"])
        return _OfflineAllow()

    monkeypatch.setattr(opa_module.opa_client, "evaluate", _evaluate)

    started: list = []

    async def _fake_deploy(request, record=None):
        started.append(request)
        return record

    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)

    body = {
        "project_id": "p",
        "cluster": "recoder-cluster",
        "service": "recoder-app",
        "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/recoder-app:v1",
        "region": "us-east-1",
        "run_security_scan": False,
        "generate_sbom": False,
        "environment": "production",
        "branch": "feat/evil",
    }

    for level in (1, 2, 3, 4):
        ecs_routes._deploy_records.clear()
        resp = client.post("/api/ecs/deploy", json={**body, "approval_level": level})
        assert resp.status_code == 403, (
            f"승인 레벨 {level} 로 스캔·SBOM 없는 프로덕션 배포가 통과했다: "
            f"{resp.status_code} {resp.text}"
        )
    assert not started, "게이트를 못 넘었는데 파이프라인이 시작됐다"
    assert min(seen_levels) >= ecs_routes.ECS_DEPLOY_MIN_APPROVAL_LEVEL, (
        f"요청이 보낸 낮은 승인 레벨이 그대로 정책 평가에 넘어갔다: {seen_levels}"
    )

    # 반대 방향 — 정상 요청은 레벨 1 로 보내도 통과해야 한다.
    ecs_routes._deploy_records.clear()
    ok = client.post("/api/ecs/deploy", json={
        **body, "approval_level": 1, "run_security_scan": True,
        "generate_sbom": True, "environment": "", "branch": "",
    })
    assert ok.status_code == 202, (
        f"OPA 없이도 통과해야 하는 정상 배포까지 막았다: {ok.status_code} {ok.text}"
    )


def test_the_local_gate_is_not_looser_than_the_real_rego_policy():
    """[불변식] 폴백은 대신 판단하는 것이지 **더 봐주는 것**이 아니다.

    실제 Rego 프리셋(`PROD_MAIN_BRANCH_ONLY`)은 `branch != "main"` 이면
    거부한다. 로컬 폴백만 `master` 를 열어 두면, OPA 가 떠 있느냐에 따라
    같은 배포의 결과가 뒤집힌다 — 그건 정책이 아니라 우연이다.

    허용 목록을 여기 박아 두지 않고 Rego 원문에서 뽑아 비교한다. 박아 두면
    한쪽만 바뀔 때 이 테스트가 같이 낡아서 아무것도 못 잡는다.
    """
    import re

    from core.opa_gate import production_branches

    rego_path = (Path(__file__).resolve().parents[2] / "control_plane" / "services"
                 / "policy_service.py")
    if not rego_path.is_file():
        pytest.skip("control_plane 이 없는 배포본이다")
    rego = rego_path.read_text(encoding="utf-8")
    snippet = rego.split("PolicyPresetKey.PROD_MAIN_BRANCH_ONLY", 1)[1].split('"""', 2)[1]
    allowed = set(re.findall(r'input\.context\.branch\s*!=\s*"([^"]+)"', snippet))
    assert allowed, f"Rego 프리셋에서 허용 브랜치를 못 읽었다: {snippet!r}"

    default = production_branches()
    # 위쪽 경계 — 폴백이 Rego 보다 느슨하면 OPA 가용성에 따라 결과가 뒤집힌다.
    assert set(default) <= allowed, (
        f"로컬 폴백이 Rego 보다 느슨하다. 폴백={default} Rego={sorted(allowed)}"
    )
    # 아래쪽 경계 — 이게 없으면 목록을 비워 **모든 프로덕션 배포를 막아도**
    # 위 단언은 그대로 통과한다. 과잉 차단도 규칙이 깨진 것이다.
    assert set(default) == allowed, (
        f"폴백이 Rego 보다 좁다 — OPA 가 켜지면 통과할 배포를 지금 막고 있다: "
        f"폴백={default} Rego={sorted(allowed)}"
    )


def test_a_master_default_repository_has_a_way_out(monkeypatch):
    """[사용성] 기본 브랜치가 `master` 인 팀이 제품 안에서 막히면 안 된다.

    OPA 를 띄우는 곳이 리포에 없으므로 이 폴백이 **기본 경로**다. `main`
    하나만 허용하면 `master` 저장소는 프로덕션 배포를 아예 못 하는데,
    "main 으로 머지하세요"는 그 팀에게 실행 불가능한 안내다.

    조건을 넓히는 손잡이는 **요청 본문이 아니라 서버 설정**이어야 한다 —
    호출자가 게이트 조건을 고를 수 있으면 그건 게이트가 아니다.
    """
    from core.api.routes import ecs as ecs_routes
    from core.opa_gate import ENV_PRODUCTION_BRANCHES

    def judge(branch):
        return ecs_routes._local_policy_fallback(
            ECSDeployRecord(),
            make_request(environment="production", branch=branch,
                         generate_sbom=True, run_security_scan=True),
        )

    monkeypatch.delenv(ENV_PRODUCTION_BRANCHES, raising=False)
    denied = judge("master")
    assert denied.decision != "allow"
    assert ENV_PRODUCTION_BRANCHES in (denied.fix_suggestion or ""), (
        f"막기만 하고 빠져나갈 방법을 안 알려준다: {denied.fix_suggestion}"
    )

    monkeypatch.setenv(ENV_PRODUCTION_BRANCHES, "main,master")
    assert judge("master").decision == "allow"
    assert judge("feat/x").decision != "allow", "넓히랬더니 통째로 열렸다"

    # 빈 값으로 설정해 전부 막아 버리는 실수는 기본값으로 되돌린다.
    monkeypatch.setenv(ENV_PRODUCTION_BRANCHES, "  ,  ")
    assert judge("main").decision == "allow"


def test_a_missing_workspace_is_recorded_not_swallowed(tmp_path):
    """[회귀] 못 돌린 검사는 **기록에 남아야** 한다.

    작업 폴더가 없으면 시크릿·Dockerfile 검사를 할 수 없다(이미지만 다시
    올리는 경우). 예전에는 로그만 남기고 조용히 넘어갔다 — 사이드바는
    로그가 아니라 `provisioned` 를 읽으므로, 사용자에게는 "보안 검사 켜고
    배포 성공"으로만 보였다.
    """
    rec = ECSDeployRecord()
    out = asyncio.run(
        ECSAgent()._step_scan_sources(make_request(workspace_path=None), rec)
    )
    assert out.provisioned.get("scan_warning"), (
        "소스 검사를 건너뛰었는데 기록에 아무 말이 없다"
    )
    assert "시크릿" in out.provisioned["scan_warning"]


def test_a_missing_dockerfile_is_recorded_too(tmp_path, monkeypatch):
    """[회귀] Dockerfile 이 없으면 hadolint 는 돌 수 없다 — 그것도 남겨야 한다.

    작업 폴더는 있는데 Dockerfile 만 없는 경우가 실제로 흔하다(경로를 다르게
    쓰거나 하위 폴더에 둔 경우). 조용히 넘어가면 "Dockerfile 검사 통과"로
    보인다.
    """
    from core.schemas import SecurityScanResult

    async def _scan_all(**_kwargs):
        return SecurityScanResult(image=None, findings=[], tool_errors=[])

    monkeypatch.setattr(
        "core.agents.ecs_agent.security_scanner.scan_all", _scan_all
    )

    rec = ECSDeployRecord()
    out = asyncio.run(
        ECSAgent()._step_scan_sources(
            make_request(workspace_path=str(tmp_path)), rec
        )
    )
    assert "Dockerfile" in (out.provisioned.get("scan_warning") or ""), (
        f"Dockerfile 이 없는데 아무 말이 없다: {out.provisioned}"
    )


def test_two_different_scan_gaps_do_not_erase_each_other():
    """[회귀] 뒤에 생긴 경고가 앞의 경고를 덮어쓰면 빠진 검사 하나가 사라진다."""
    from core.schemas import SecurityScanResult

    agent = ECSAgent()
    rec = ECSDeployRecord()
    agent._add_scan_gaps(rec, ["작업 폴더 없음 — 시크릿·Dockerfile 검사 불가"])
    rec.scan_result = SecurityScanResult(
        image="img:1", tool_errors=["trivy 미설치"], findings=[]
    )
    agent._record_scan_gaps(rec)
    warning = rec.provisioned["scan_warning"]
    assert "시크릿" in warning and "trivy" in warning, (
        f"경고 하나가 사라졌다: {warning}"
    )


def test_the_same_scan_gap_is_not_reported_twice():
    """[회귀] 같은 말을 두 번 하면 사용자는 새 문제가 생긴 줄 안다.

    빌드 전·후로 두 번 검사하는데, 두 번째의 `tool_errors` 는 첫 번째의
    **상위 집합**이다(결과가 누적되고 `compute_pass()` 가 다시 계산된다).
    문자열을 그냥 이어 붙이면, 스캐너가 하나도 안 깔린 **가장 흔한
    상황**에서 같은 경고가 두 줄로 나온다.
    """
    from core.schemas import SecurityScanResult

    agent = ECSAgent()
    rec = ECSDeployRecord()

    rec.scan_result = SecurityScanResult(
        image=None, tool_errors=["hadolint 미설치", "gitleaks 미설치"], findings=[]
    )
    agent._record_scan_gaps(rec)

    # 두 번째 호출 — 첫 번째 목록을 포함한 더 긴 목록
    rec.scan_result = SecurityScanResult(
        image="img:1",
        tool_errors=["hadolint 미설치", "gitleaks 미설치", "trivy 미설치"],
        findings=[],
    )
    agent._record_scan_gaps(rec)

    warning = rec.provisioned["scan_warning"]
    assert warning.count("hadolint 미설치") == 1, f"같은 경고가 중복됐다: {warning}"
    assert warning.count("gitleaks 미설치") == 1, f"같은 경고가 중복됐다: {warning}"
    assert "trivy 미설치" in warning, warning
    assert warning.count("실행되지 않은 보안 검사") == 1, warning


def test_the_policy_fallback_warning_survives_the_sidebar_truncation():
    """[회귀] 확장은 `log_tail` 의 **끝 세 줄만** 보여준다.

    "정책 엔진 없이 로컬 규칙으로 판단했다"는 사용자가 반드시 봐야 할
    경고인데 `_WARNING_KEYS` 에 없으면 앞쪽 사실들 사이에 섞여 잘려 나간다.
    경고를 남기는 코드와 경고를 뒤로 미는 목록이 따로 놀면, 남긴 사람은
    남겼다고 믿고 사용자는 못 보는 상태가 된다.
    """
    from core.api.routes.deploy_ecs import to_status_response

    rec = ECSDeployRecord(status=ECSDeployStatus.SUCCEEDED)
    # 경고를 **맨 앞에** 넣는다. 실제로 그렇게 들어간다 — 정책 판단은
    # 파이프라인이 시작되기 전이고, 클러스터·서브넷 같은 사실은 그 뒤에
    # 쌓인다. 뒤에 넣고 확인하면 dict 순서 덕에 우연히 통과한다.
    rec.provisioned["policy_warning"] = (
        "정책 엔진(OPA)에 연결하지 못해 로컬 규칙으로 판단했습니다."
    )
    rec.provisioned.update({
        "cluster": "recoder-cluster",
        "service": "recoder-app",
        "subnets": "subnet-1,subnet-2",
        "log_group": "/ecs/recoder",
        "task_definition": "arn:aws:ecs:us-east-1:1:task-definition/recoder:7",
    })
    tail = to_status_response(rec).log_tail
    assert any("policy_warning" in line for line in tail[-3:]), (
        f"경고가 확장이 보여주는 마지막 세 줄 밖으로 밀려났다: {tail}"
    )


def test_the_service_down_warning_survives_the_sidebar_truncation():
    """[회귀] "앱이 내려가 있다"는 잘려 나가면 안 되는 종류의 말이다.

    태스크 0 개 배포를 취소하면 기존 서비스가 0 으로 내려간 채 끝난다.
    그 사실이 `log_tail` 앞쪽 사실들 사이에 섞여 잘리면, 사용자는 앱이
    죽은 줄도 모른 채 화면을 닫는다.
    """
    from core.api.routes.deploy_ecs import to_status_response

    rec = ECSDeployRecord(status=ECSDeployStatus.CANCELLED)
    rec.provisioned["service_warning"] = (
        "취소했지만 이 배포는 이미 기존 서비스의 태스크 수를 0 으로 바꾼 "
        "뒤였습니다 — 원래 앱이 내려가 있습니다."
    )
    rec.provisioned.update({
        "cluster": "recoder-cluster",
        "service": "recoder-app (updated)",
        "subnets": "subnet-1,subnet-2",
        "log_group": "/ecs/recoder",
        "task_definition": "arn:aws:ecs:us-east-1:1:task-definition/recoder:7",
    })
    tail = to_status_response(rec).log_tail
    assert any("service_warning" in line for line in tail[-3:]), (
        f"앱이 내려갔다는 경고가 마지막 세 줄 밖으로 밀려났다: {tail}"
    )


def test_scan_gap_bookkeeping_does_not_destroy_the_saved_record(tmp_path, monkeypatch):
    """[회귀] `provisioned` 에 list 를 넣으면 **기록이 통째로 사라진다.**

    `provisioned` 는 `dict[str, str]` 이다. 파이단틱은 dict 안쪽에 in-place
    로 넣는 값까지 검사하지 않으므로, list 를 넣어도 **쓸 때는 아무 일도
    일어나지 않는다.** 대신 `_load_records()` 가 다시 읽을 때
    ValidationError 가 나고 `except → continue` 가 그 기록을 버린다.

    스캐너가 하나도 안 깔린 상태가 가장 흔하므로 거의 모든 배포가 이 값을
    남긴다. 그 상태에서 코어를 재시작하면 진행 중이던 배포 기록이 사라지고,
    비용 경고도 클러스터·서비스 이름도 함께 없어져 **떠 있는 Fargate 태스크를
    제품 안에서 멈출 방법이 없어진다** — `_save_records` 가 막으려던 바로 그
    상황이다.
    """
    from core.api.routes import ecs as ecs_routes
    from core.api.routes.deploy_ecs import to_status_response

    rec = ECSDeployRecord(
        deployment_id="d1", cluster="c", service="s",
        status=ECSDeployStatus.IN_PROGRESS,
    )
    ECSAgent()._add_scan_gaps(rec, ["trivy 미설치", "gitleaks 미설치"])

    # ① `provisioned` 의 문자열 계약을 깨지 않는다
    for key, value in rec.provisioned.items():
        assert isinstance(value, str), (
            f"provisioned[{key!r}] 가 문자열이 아니다: {value!r} — "
            "재기동 시 이 기록이 통째로 버려진다"
        )

    # ② 저장 → 재기동 → 로드 왕복에서 살아남는다
    monkeypatch.setenv("RECODER_ECS_STORE", str(tmp_path / "store.json"))
    monkeypatch.setattr(ecs_routes, "_deploy_records", {"d1": rec})
    ecs_routes._save_records()
    loaded = ecs_routes._load_records()
    assert "d1" in loaded, "재기동 후 배포 기록이 사라졌다 — 떠 있는 태스크를 못 멈춘다"
    assert loaded["d1"].scan_gaps == ["trivy 미설치", "gitleaks 미설치"]

    # ③ 사용자에게는 문구 하나만 보인다 (문구를 만든 재료는 안 보인다)
    tail = to_status_response(rec).log_tail
    assert not any(line.startswith("scan_gaps:") for line in tail), tail
    assert any("scan_warning" in line for line in tail), tail


def test_scans_off_is_actually_reachable_through_the_deploy_route():
    """[메타] 위 테스트가 지키는 구멍이 **실제로 열려 있었는지** 확인한다.

    `run_security_scan=False` 를 아무도 보낼 수 없다면 위 테스트는 아무것도
    지키지 않는다. 코어 요청 모델이 그 값을 받고, 파이프라인이 그때 스캔
    단계를 건너뛴다는 두 가지가 모두 사실이어야 의미가 있다.
    """
    import inspect

    assert make_request(run_security_scan=False).run_security_scan is False

    source = inspect.getsource(ECSAgent._deploy_pipeline)
    assert source.count("if request.run_security_scan:") == 2, (
        "파이프라인의 스캔 단계 두 개가 이 플래그로 갈리지 않는다 — "
        "테스트가 지키려는 구멍이 여기가 아니게 됐다"
    )


def test_require_ecr_repository_rejects_an_empty_listing():
    """AWS 는 없는 리포지토리에 예외를 던지지만, 빈 목록으로 답하는
    구현(모의 서버·프록시)도 있다. 그때 IndexError 로 터지면 안 된다."""
    class _Empty:
        @staticmethod
        def describe_repositories(**_):
            return {"repositories": []}

    with pytest.raises(aws_infra.InfraError) as caught:
        aws_infra.require_ecr_repository(_Empty(), "recoder-app")
    assert "recoder-app" in caught.value.message


def test_the_branch_is_derived_from_the_workspace_when_the_caller_omits_it(tmp_path):
    """[회귀] 배관만 고치고 값을 만드는 곳을 안 고치면 규칙은 여전히 무력하다.

    확장 배포 화면에는 브랜치 입력칸이 아예 없고 사이드바는 빈 문자열을
    보낸다. 그래서 "프로덕션은 main 에서만" 규칙이 판단 재료를 못 받았다.
    """
    from core import branch_source

    repo = tmp_path / "ws"
    init_git_repo(repo, branch="feat/xyz")

    assert branch_source.resolve_branch(str(repo), "") == "feat/xyz", (
        "작업 폴더에서 브랜치를 못 알아냈다"
    )


def test_an_explicit_branch_is_used_when_git_cannot_tell(tmp_path):
    """[부정 통제] 관측할 수 없을 때는 호출자가 준 값을 쓴다.

    git 저장소가 아닌 폴더나 워크스페이스 없는 CI 호출에서는 이것 말고
    브랜치를 알 방법이 없다. 여기까지 막으면 아무도 못 쓴다.
    """
    from core import branch_source
    from core.api.routes.deploy_ecs import ExtensionEcsDeployRequest, to_core_request

    assert branch_source.resolve_branch(str(tmp_path), "main") == "main"
    assert branch_source.resolve_branch(None, "main") == "main"
    core_request = to_core_request(
        ExtensionEcsDeployRequest(workspace_path=str(tmp_path), branch="main")
    )
    assert core_request.branch == "main"


def test_a_claimed_branch_cannot_override_the_real_one(tmp_path):
    """[보안 · 회귀] 자기 신고가 증거를 이기면 규칙이 아니다.

    작업 폴더는 `feat/evil` 인데 요청 본문에 `branch: main` 한 줄만 넣으면
    "프로덕션은 main 에서만" 규칙이 그대로 통과했다. 승인 레벨을 본문에서
    못 내리게 막아 놓고 브랜치는 자기 신고를 믿으면, 같은 구멍이 이름만
    바꿔 남아 있는 셈이다.
    """
    from core import branch_source
    from core.api.routes import ecs as ecs_routes

    repo = tmp_path / "ws"
    init_git_repo(repo, branch="feat/evil")

    resolved = branch_source.resolve_branch(str(repo), "main")
    assert resolved == "feat/evil", (
        f"요청이 신고한 브랜치가 작업 폴더의 실제 브랜치를 덮어썼다: {resolved!r}"
    )

    result = ecs_routes._local_policy_fallback(
        ECSDeployRecord(),
        make_request(environment="production", branch=resolved,
                     generate_sbom=True, run_security_scan=True),
    )
    assert result is not None and result.decision != "allow", (
        "branch 를 main 이라고 신고한 것만으로 프로덕션 배포가 통과했다"
    )


@pytest.mark.parametrize("env_name", ["production", "Production", "PRODUCTION"])
def test_the_production_rule_is_not_defeated_by_capitalisation(env_name):
    """[회귀] 환경 이름 대소문자 하나로 게이트가 통째로 사라지면 안 된다."""
    from core.api.routes import ecs as ecs_routes

    result = ecs_routes._local_policy_fallback(
        ECSDeployRecord(),
        make_request(environment=env_name, branch="feat/x",
                     generate_sbom=True, run_security_scan=True),
    )
    assert result is not None and result.decision != "allow", (
        f"environment={env_name!r} 로 프로덕션 규칙을 우회했다"
    )


@pytest.mark.parametrize("env_name", ["Production", "PRODUCTION", " production "])
def test_the_environment_is_normalised_before_it_reaches_either_engine(
    env_name, app_client, monkeypatch
):
    """[회귀] 로컬 폴백에서만 대소문자를 접으면 **OPA 가 켜졌을 때** 뚫린다.

    실제 Rego 프리셋은 `input.context.environment == "production"` 을 정확히
    비교한다. 폴백에서만 접으면, OPA 가 꺼져 있을 땐 막히고 켜져 있을 땐
    통과하는 배포가 생긴다 — 같은 요청의 결과가 인프라 상태에 따라 뒤집힌다.
    그래서 요청이 라우트에 들어오는 지점에서 값을 정규화한다.
    """
    from core import opa_client as opa_module

    client, ecs_routes = app_client
    seen: list[dict] = []

    class _Allow:
        decision, reason, fix_suggestion = "allow", "", None
        opa_available, required_approvers = True, 0

    async def _evaluate(**kwargs):
        seen.append(kwargs["context"])
        return _Allow()

    async def _fake_deploy(request, record=None):
        return record

    monkeypatch.setattr(opa_module.opa_client, "evaluate", _evaluate)
    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)

    resp = client.post("/api/ecs/deploy", json={
        "project_id": "p", "cluster": "recoder-cluster", "service": "recoder-app",
        "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/recoder-app:v1",
        "region": "us-east-1", "environment": env_name, "branch": "main",
    })
    assert resp.status_code == 202, resp.text
    assert seen and seen[0]["environment"] == "production", (
        f"정책 엔진이 정규화되지 않은 환경 이름을 봤다: {seen[0]['environment']!r}"
    )


def test_the_core_route_also_refuses_a_claimed_branch(tmp_path, app_client, monkeypatch):
    """[보안 · 회귀] 브랜치 검증이 **확장용 변환기에만** 있으면 한쪽 문이 열려 있다.

    `/api/deploy/ecs`(확장)는 `to_core_request` 를 거치지만,
    `/api/ecs/deploy`(디스코드 봇·직접 호출)는 `ECSDeployRequest` 를 그대로
    받는다. 변환기에서만 브랜치를 검증하면 후자에서는 `{"branch": "main"}`
    한 줄로 프로덕션 게이트가 그냥 통과한다 — 고친 문 옆에 안 고친 문이
    나란히 있는 셈이다. 두 경로의 합류점인 이 라우트에서 확정해야 한다.
    """
    from core import opa_client as opa_module

    client, ecs_routes = app_client
    repo = tmp_path / "ws"
    init_git_repo(repo, branch="feat/evil")

    class _OfflineDeny:
        decision, reason, fix_suggestion = "deny", "OPA 오프라인", None
        opa_available, required_approvers = False, 0

    async def _evaluate(**_kwargs):
        return _OfflineDeny()

    started: list = []

    async def _fake_deploy(request, record=None):
        started.append(request)
        return record

    monkeypatch.setattr(opa_module.opa_client, "evaluate", _evaluate)
    monkeypatch.setattr(ecs_routes._ecs_agent, "deploy", _fake_deploy)

    resp = client.post("/api/ecs/deploy", json={
        "project_id": "p", "cluster": "recoder-cluster", "service": "recoder-app",
        "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/recoder-app:v1",
        "region": "us-east-1", "workspace_path": str(repo),
        "environment": "production", "branch": "main",
        "run_security_scan": True, "generate_sbom": True,
    })
    assert resp.status_code == 403, (
        f"작업 폴더는 feat/evil 인데 branch=main 신고만으로 통과했다: "
        f"{resp.status_code} {resp.text}"
    )
    assert not started, "게이트를 못 넘었는데 파이프라인이 시작됐다"

    # 거부도 기록에 남아야 한다. 성공 경로에서만 저장하면 코어를 다시 켰을 때
    # "왜 막혔는지"가 통째로 사라지고, 사용자는 같은 벽에 다시 부딪힌다.
    saved = ecs_routes._load_records()
    assert saved, "정책 거부 기록이 저장되지 않았다"
    denied = list(saved.values())[-1]
    assert denied.status == ECSDeployStatus.FAILED
    assert "정책" in (denied.error_message or ""), denied.error_message
    # 사유만 남기고 **고치는 법**과 끝난 시각을 빼면 반쪽이다 — 사이드바는
    # 그 둘을 읽고, 비면 "언제 왜 실패했는지 모르는" 카드가 된다.
    assert denied.error_remedy, "왜 막혔는지만 있고 어떻게 풀지가 없다"
    assert denied.completed_at is not None, "끝난 시각이 비어 있다"


def test_an_untracked_subdirectory_still_cannot_launder_the_branch(tmp_path):
    """[보안 · 회귀] 추적 여부로 "관측 실패"를 판정하면 **더 큰 구멍**이 된다.

    `git rev-parse` 는 상위 폴더로 거슬러 올라가 가장 가까운 저장소를 찾는다.
    그게 무관한 저장소일 수 있다는 이유로 "이 폴더가 추적되는가"(`git
    ls-files`)를 조건으로 걸었더니, 정반대의 결과가 나왔다.

      - 작업 폴더를 `.gitignore` 에 한 줄 넣거나 아직 커밋만 안 해도
        "관측 실패"가 되고, 규칙이 **호출자의 신고를 믿는 쪽으로** 넘어간다.
        즉 `.gitignore` 한 줄로 브랜치 검사를 통째로 우회할 수 있었다.
      - `git ls-files` 는 인덱스를 갱신하면서 저장소의 `core.fsmonitor`
        프로그램을 실행한다 — 남이 준 `.git` 이 섞인 폴더를 배포하면 임의
        실행이 된다. `rev-parse` 는 그러지 않는다.

    **모호하면 관측값 쪽으로 기운다.** 상위 저장소의 브랜치라도 그건 호출자가
    고를 수 없는 값이라, 신고로 떨어지는 것보다 항상 안전하다.
    """
    from core import branch_source

    repo = tmp_path / "repo"
    init_git_repo(repo, branch="feat/evil")
    (repo / ".gitignore").write_text("app/\n", encoding="utf-8")
    app = repo / "app"
    app.mkdir()
    (app / "main.py").write_text("x", encoding="utf-8")

    assert branch_source.resolve_branch(str(app), "main") == "feat/evil", (
        "무시(.gitignore)되는 폴더라는 이유로 신고한 브랜치를 믿었다 — "
        "gitignore 한 줄로 프로덕션 게이트가 열린다"
    )

    # 아직 커밋 전인 새 폴더도 마찬가지 — 관측값이 있으면 관측값을 쓴다.
    fresh = repo / "brand_new"
    fresh.mkdir()
    (fresh / "main.py").write_text("x", encoding="utf-8")
    assert branch_source.resolve_branch(str(fresh), "main") == "feat/evil"

    # 모노레포의 추적되는 하위 폴더도 당연히 인정한다.
    tracked = repo / "apps" / "api"
    tracked.mkdir(parents=True)
    (tracked / "main.py").write_text("x", encoding="utf-8")
    assert branch_source.current_branch(str(tracked)) == "feat/evil"


def test_the_branch_lookup_does_not_run_repository_supplied_programs(
    tmp_path, monkeypatch
):
    """[보안] 배포 대상 폴더의 `.git/config` 가 **바깥 프로그램을 실행**하면 안 된다.

    `core.fsmonitor` / `core.hooksPath` 는 저장소가 들고 다니는 설정이고,
    `git ls-files` 나 `git commit` 같은 하위 명령이 그 프로그램을 실행한다.
    남이 준 프로젝트 압축에는 `.git` 이 딸려 오는 일이 흔한데, 그 폴더를
    배포하면 이 코드는 HTTP 핸들러 안에서 돈다.

    지금 쓰는 `rev-parse` 는 그 설정을 실행하지 않지만, **나중에 하위 명령이
    하나라도 늘면 즉시 실행 경로가 된다**(실제로 그런 갈래를 넣었다가
    되돌렸다). 그래서 "어떤 git 호출이든 저장소 설정으로 바깥 프로그램을
    실행하지 않는다"를 호출 규약으로 못 박는다.
    """
    from core import branch_source

    repo = tmp_path / "repo"
    init_git_repo(repo, branch="main")

    calls: list[list[str]] = []
    real = branch_source.subprocess.run

    def _spy(argv, *args, **kwargs):
        calls.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(branch_source.subprocess, "run", _spy)
    assert branch_source.current_branch(str(repo)) == "main"

    assert calls, "git 을 아예 부르지 않았다"
    for argv in calls:
        joined = " ".join(argv)
        assert "core.fsmonitor=" in joined, (
            f"저장소가 지정한 fsmonitor 프로그램이 실행될 수 있다: {joined}"
        )
        assert "core.hooksPath=" in joined, (
            f"저장소가 지정한 훅이 실행될 수 있다: {joined}"
        )

    # 상속된 `GIT_DIR` / `GIT_WORK_TREE` 는 **cwd 를 무시하게 만든다.**
    # 코어가 git 훅이나 래퍼에서 실행되면 실제로 그런 값이 들어온다.
    monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/somewhere/else")
    leaked = [k for k in branch_source._git_env()
              if k.startswith("GIT_")
              and k not in ("GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS")]
    assert not leaked, f"git 환경변수가 그대로 상속된다: {leaked}"
    assert branch_source.current_branch(str(repo)) == "main", (
        "상속된 GIT_DIR 때문에 엉뚱한 저장소를 봤다"
    )


def test_a_non_git_workspace_does_not_break_the_request(tmp_path):
    """git 저장소가 아니어도 **요청 자체는** 만들어져야 한다.

    빈 브랜치는 "제한 없음"이 아니라 "모름"이다. 여기서는 요청이 400 으로
    터지지 않는 것만 본다 — 그 "모름"이 프로덕션에서 거부로 이어지는지는
    `test_production_is_denied_when_the_branch_cannot_be_proven` 이 지킨다.
    (예전에는 이 테스트만 있어서, 빈 브랜치가 통과로 이어지는 것을 오히려
    정상으로 못 박고 있었다.)
    """
    from core.api.routes.deploy_ecs import ExtensionEcsDeployRequest, to_core_request

    core_request = to_core_request(
        ExtensionEcsDeployRequest(workspace_path=str(tmp_path))
    )
    assert core_request.branch == ""


def test_the_sbom_generator_falls_back_to_docker_when_syft_is_missing():
    """[회귀] `syft` 바이너리는 **어느 개발자 PC 에도 없다.**

    SETUP.md 준비물은 Python·Node·Docker Desktop 뿐이고 CI 도 설치하지
    않는다. 그 상태에서 SBOM 실패를 배포 실패로 올리면 **모든 배포가
    막힌다.** 도커는 어차피 있어야 하므로 컨테이너로 돌린다.
    """
    import unittest.mock as mock
    from pathlib import Path

    from core.sbom import SBOMGenerator

    with mock.patch("core.sbom.shutil.which", return_value="/usr/bin/syft"):
        native = SBOMGenerator._syft_command("img:1", Path("/out/a.json"))
    assert native[0] == "syft"

    with mock.patch("core.sbom.shutil.which", return_value=None):
        fallback = SBOMGenerator._syft_command("img:1", Path("/out/a.json"))
    assert fallback[0] == "docker", "syft 가 없는데 대체 경로가 없다"
    assert "anchore/syft" in " ".join(fallback)
    assert "docker:img:1" in fallback, "로컬 도커 데몬의 이미지를 가리켜야 한다"
    assert "/var/run/docker.sock:/var/run/docker.sock" in fallback, (
        "도커 소켓을 안 넘기면 컨테이너가 로컬 이미지를 못 읽는다"
    )

    # 결과 마운트는 **플랫폼마다 호스트 경로 모양이 다르다.** 리눅스 기준
    # 문자열을 박아두면 윈도우에서만 깨진다 — 개발 PC 가 전부 윈도우인데.
    mounts = [fallback[i + 1] for i, a in enumerate(fallback) if a == "-v"]
    out_mount = [m for m in mounts if m.endswith(":/out")]
    assert out_mount, f"결과 파일을 호스트로 못 받는다: {mounts}"
    host_dir = out_mount[0][: -len(":/out")]
    assert "\\" not in host_dir, (
        f"호스트 경로에 역슬래시가 섞였다 — 윈도우에서 마운트가 깨진다: {host_dir}"
    )
    assert host_dir.endswith("out"), host_dir


def test_the_sbom_failure_reason_reaches_the_record():
    """[회귀] `_empty_record` 가 원인을 받아놓고 모델에 안 넣었다.

    그래서 호출부는 항상 기본 문구만 보여줬고, 진짜 원인(타임아웃·권한
    거부)은 사용자에게 닿지 않았다.
    """
    from core.sbom import SBOMGenerator

    record = SBOMGenerator._empty_record("img:1", error="syft timeout (120s)")
    assert record.error == "syft timeout (120s)", (
        "실패 원인이 기록에서 사라졌다"
    )


def test_the_branch_lookup_cannot_hang_the_request(monkeypatch, tmp_path):
    """git 이 멈추면 배포 요청 전체가 멈춘다 — 시간 제한이 있어야 한다.

    브랜치 조회는 HTTP 핸들러가 부르는 동기 호출이다. 여기서 git 이
    자격증명 프롬프트 같은 걸로 붙잡히면 요청이 영영 안 끝난다.
    """
    from core import branch_source

    seen: list[dict] = []
    real = branch_source.subprocess.run

    def _spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(branch_source.subprocess, "run", _spy)
    branch_source.current_branch(str(tmp_path))

    assert seen, "git 을 아예 부르지 않았다"
    assert all(call.get("timeout") for call in seen), (
        f"시간 제한 없는 git 호출이 있다: {seen}"
    )
    # 자격증명 프롬프트로 멈추는 것도 같은 종류의 정지다.
    assert all(
        call.get("env", {}).get("GIT_TERMINAL_PROMPT") == "0" for call in seen
    ), "git 이 자격증명 프롬프트를 띄울 수 있다 — 요청이 영영 안 끝난다"


def test_the_sbom_mount_path_survives_windows():
    """[회귀] 개발 PC 가 전부 윈도우인데 리눅스 기준으로만 짰다.

    `str(WindowsPath)` 는 `C:\\Users\\...` 로 나오고, `-v` 인자는 콜론으로
    호스트·컨테이너를 가르므로 역슬래시가 섞이면 마운트가 깨진다.
    테스트는 리눅스에서 통과하고 **실제 배포만 윈도우에서 깨진다.**
    """
    import unittest.mock as mock
    from pathlib import PureWindowsPath

    from core.sbom import SBOMGenerator

    with mock.patch("core.sbom.shutil.which", return_value=None):
        cmd = SBOMGenerator._syft_command(
            "img:1", PureWindowsPath(r"C:\Users\dy981\.recoder\sbom\a.json")
        )

    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    out_mount = next(m for m in mounts if m.endswith(":/out"))
    assert out_mount == "C:/Users/dy981/.recoder/sbom:/out", out_mount
    assert "\\" not in out_mount, "역슬래시가 남아 도커가 경로를 못 읽는다"


# ===========================================================================
# 인터넷 경로 판정 — Codex 5차 P2
#
# `igw-` 로 시작하는 게이트웨이가 **보이기만 하면** 공인 서브넷으로 쳤다.
# 좁은 경로나 blackhole 도 통과한다. 그러면 Fargate 태스크가 그 서브넷에
# 배치되고 ECR 에서 이미지를 못 받아 죽는다 — 이 검사가 막으려던 증상이다.
# ===========================================================================


def _routes(*routes):
    return {"Associations": [{"Main": True}], "Routes": list(routes)}


def test_a_narrow_igw_route_is_not_an_internet_path():
    """[회귀] 특정 대역만 가는 경로는 인터넷 전반으로 못 나간다."""
    table = _routes(
        {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"},
        {"DestinationCidrBlock": "52.94.0.0/16", "GatewayId": "igw-123"},
    )
    assert aws_infra._route_table_has_igw(table) is False, (
        "좁은 경로를 인터넷 경로로 오판했다"
    )


def test_a_blackholed_igw_route_is_not_an_internet_path():
    """[회귀] 게이트웨이가 떨어져 나간 경로는 남아 있어도 아무 데도 안 간다."""
    table = _routes(
        {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-123",
         "State": "blackhole"},
    )
    assert aws_infra._route_table_has_igw(table) is False


def test_a_real_default_route_is_accepted():
    """[부정 통제] 진짜 기본 경로는 통과해야 한다."""
    assert aws_infra._route_table_has_igw(_routes(
        {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-123",
         "State": "active"},
    )) is True
    # IPv6 기본 경로도 인정한다.
    assert aws_infra._route_table_has_igw(_routes(
        {"DestinationIpv6CidrBlock": "::/0", "GatewayId": "igw-123"},
    )) is True
    # State 를 안 주는 구현(moto)은 활성으로 본다 — 여기서 막으면
    # 멀쩡한 테스트 환경과 일부 실계정 응답이 전부 막힌다.
    assert aws_infra._route_table_has_igw(_routes(
        {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-123"},
    )) is True


# ===========================================================================
# ECS 롤백 제안 → 사람 승인 — 자동 실행 금지
# ===========================================================================


def _pending_ecs_rollback_record():
    """승인을 기다리는 health-failure 기록 하나를 만든다."""
    record = ECSDeployRecord(
        deployment_id="rollback-deployment",
        cluster="recoder-cluster",
        service="recoder-app",
        region="ap-northeast-2",
        status=ECSDeployStatus.FAILED,
        task_definition_arn="arn:aws:ecs:ap-northeast-2:123:task-definition/app:4",
        previous_task_definition_arn="arn:aws:ecs:ap-northeast-2:123:task-definition/app:3",
        rollback_proposal_id="rollback-deployment",
        rollback_approval_level=3,
        rollback_proposal_status="pending",
        health_check_failures=3,
        error_message="배포 Health Check 실패 — 롤백 제안을 만들었습니다",
    )
    return record


def test_ecs_rollback_status_exposes_a_pending_proposal_without_aws_call(app_client, monkeypatch):
    """[D17] Watchdog 감지/상태 조회만으로는 절대 롤백하지 않는다."""
    from api.routes import deploy_ecs

    client, ecs_routes = app_client
    record = _pending_ecs_rollback_record()
    ecs_routes._deploy_records[record.deployment_id] = record
    called = []
    monkeypatch.setattr(
        deploy_ecs.aws_infra,
        "revert_service_task_definition",
        lambda *_args, **_kwargs: called.append(True),
    )

    response = client.get("/api/deploy/ecs/status")
    assert response.status_code == 200, response.text
    proposal = response.json()["rollback_proposal"]
    assert proposal["proposal_id"] == record.rollback_proposal_id
    assert proposal["status"] == "pending"
    assert proposal["previous_task_definition"] == record.previous_task_definition_arn
    assert called == [], "상태 조회가 ECS UpdateService를 호출하면 자동 롤백이다"


def test_ignoring_ecs_rollback_never_calls_aws(app_client, monkeypatch):
    """[D17] 무시 버튼은 ECS를 건드리지 않고 감사 기록만 남긴다."""
    from api.routes import deploy_ecs

    client, ecs_routes = app_client
    record = _pending_ecs_rollback_record()
    ecs_routes._deploy_records[record.deployment_id] = record
    called = []
    monkeypatch.setattr(
        deploy_ecs.aws_infra,
        "revert_service_task_definition",
        lambda *_args, **_kwargs: called.append(True),
    )

    response = client.post(
        "/api/deploy/ecs/rollback",
        json={"proposal_id": record.rollback_proposal_id, "approved": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ignored"
    assert called == []
    assert record.rollback_proposal_status == "ignored"
    assert "변경하지 않았습니다" in record.provisioned["rollback_approval"]


def test_approved_ecs_rollback_updates_the_previous_task_definition(app_client, monkeypatch):
    """[D17] 명시적 승인 한 번이 있어야만 이전 Task Definition으로 되돌린다."""
    import boto3
    from api.routes import deploy_ecs

    client, ecs_routes = app_client
    record = _pending_ecs_rollback_record()
    ecs_routes._deploy_records[record.deployment_id] = record
    calls = []

    class _Session:
        def __init__(self, *, region_name):
            assert region_name == "ap-northeast-2"

        def client(self, name):
            assert name == "ecs"
            return self

        def describe_services(self, **_kwargs):
            return {"services": [{"taskDefinition": record.task_definition_arn}]}

    def _revert(_ecs, *, cluster, service, task_definition):
        calls.append((cluster, service, task_definition))
        return "이전 태스크 정의로 되돌렸습니다"

    monkeypatch.setattr(boto3.session, "Session", _Session)
    monkeypatch.setattr(deploy_ecs.aws_infra, "revert_service_task_definition", _revert)

    response = client.post(
        "/api/deploy/ecs/rollback",
        json={"proposal_id": record.rollback_proposal_id, "approved": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert calls == [("recoder-cluster", "recoder-app", record.previous_task_definition_arn)]
    assert record.rollback_proposal_status == "completed"
    assert record.status == ECSDeployStatus.ROLLED_BACK
    assert "사용자 승인" in response.json()["adr"]["content"]


def test_an_old_ecs_rollback_proposal_cannot_overwrite_a_newer_deployment(app_client, monkeypatch):
    """[P1] 서비스가 새 리비전으로 진행됐으면 오래된 승인은 차단한다."""
    import boto3
    from api.routes import deploy_ecs

    client, ecs_routes = app_client
    record = _pending_ecs_rollback_record()
    ecs_routes._deploy_records[record.deployment_id] = record
    calls = []

    class _Session:
        def __init__(self, **_kwargs):
            pass

        def client(self, _name):
            return self

        def describe_services(self, **_kwargs):
            return {"services": [{"taskDefinition": "arn:aws:ecs:ap-northeast-2:123:task-definition/app:5"}]}

    monkeypatch.setattr(boto3.session, "Session", _Session)
    monkeypatch.setattr(
        deploy_ecs.aws_infra,
        "revert_service_task_definition",
        lambda *_args, **_kwargs: calls.append(True),
    )

    response = client.post(
        "/api/deploy/ecs/rollback",
        json={"proposal_id": record.rollback_proposal_id, "approved": True},
    )
    assert response.status_code == 409, response.text
    assert calls == [], "오래된 제안이 최신 배포를 이전 리비전으로 덮어썼다"
    assert record.rollback_proposal_status == "superseded"


def test_a_reserved_new_ecs_deployment_blocks_an_old_rollback_approval(app_client, monkeypatch):
    """[P1] 새 배포가 AWS 갱신 대기 중이어도 오래된 롤백은 먼저 실행 못 한다."""
    from api.routes import deploy_ecs

    client, ecs_routes = app_client
    record = _pending_ecs_rollback_record()
    newer = ECSDeployRecord(
        deployment_id="newer-deployment",
        cluster=record.cluster,
        service=record.service,
        region=record.region,
        status=ECSDeployStatus.PENDING,
    )
    ecs_routes._deploy_records[record.deployment_id] = record
    ecs_routes._deploy_records[newer.deployment_id] = newer
    called = []
    monkeypatch.setattr(
        deploy_ecs.aws_infra,
        "revert_service_task_definition",
        lambda *_args, **_kwargs: called.append(True),
    )

    response = client.post(
        "/api/deploy/ecs/rollback",
        json={"proposal_id": record.rollback_proposal_id, "approved": True},
    )
    assert response.status_code == 409, response.text
    assert called == []
    assert record.rollback_proposal_status == "superseded"


def test_a_transient_ecs_rollback_failure_can_be_approved_again(app_client, monkeypatch):
    """[P2] 만료된 임시 자격증명을 고친 뒤 같은 제안을 재시도할 수 있다."""
    import boto3
    from api.routes import deploy_ecs

    client, ecs_routes = app_client
    record = _pending_ecs_rollback_record()
    ecs_routes._deploy_records[record.deployment_id] = record

    class _Session:
        def __init__(self, **_kwargs):
            pass

        def client(self, _name):
            return self

        def describe_services(self, **_kwargs):
            return {"services": [{"taskDefinition": record.task_definition_arn}]}

    outcomes = iter([
        "취소했지만 이전 버전으로 되돌리지 못했습니다",
        "이전 태스크 정의로 되돌렸습니다",
    ])
    monkeypatch.setattr(boto3.session, "Session", _Session)
    monkeypatch.setattr(
        deploy_ecs.aws_infra,
        "revert_service_task_definition",
        lambda *_args, **_kwargs: next(outcomes),
    )

    failed = client.post(
        "/api/deploy/ecs/rollback",
        json={"proposal_id": record.rollback_proposal_id, "approved": True},
    )
    assert failed.status_code == 502, failed.text
    assert record.rollback_proposal_status == "failed"

    retried = client.post(
        "/api/deploy/ecs/rollback",
        json={"proposal_id": record.rollback_proposal_id, "approved": True},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "completed"
