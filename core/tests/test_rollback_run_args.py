"""롤백이 배포 당시의 실행 조건을 재현하는가 — 회차4 E2E 통합 검증.

## 왜 이 테스트가 있나

롤백은 이미지 태그만 되돌리고 **포트 매핑을 잃어버리고 있었다.** 그런데 이
실패는 어디에도 빨간불이 뜨지 않았다:

  · 컨테이너 내부 헬스체크는 `127.0.0.1:8000` 을 보므로 docker 는 healthy 로 표시
  · 지속 검증(continuous verification)도 정상으로 보고
  · `/api/deploy/rollback` 도 200 을 반환

**모든 지표가 "복구됨"인데 사용자만 접속하지 못한다.** 장애 대응 중에 이걸
만나면 "롤백했는데 왜 안 되지" 로 시간을 전부 쓴다. E2E 검증에서 `docker ps`
출력의 `8000/tcp`(포트 매핑 없음)와 `0.0.0.0:18080->8000/tcp` 차이 하나로
겨우 드러났다.

그래서 여기서는 **롤백이 실제로 만드는 docker 인자**를 직접 본다. 응답 코드나
컨테이너 상태로는 이 회귀를 다시 잡을 수 없다.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

import api.routes.deploy as deploy_route
from schemas import ActionType, DeploymentPlan, DeploymentRecord, DeployMethod, DeployStatus


@pytest.fixture(autouse=True)
def clean_records():
    deploy_route._deployment_records.clear()
    yield
    deploy_route._deployment_records.clear()


@pytest.fixture()
def captured(monkeypatch):
    """docker 를 실제로 부르지 않고 인자만 모은다."""
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(deploy_route.subprocess, "run", fake_run)
    return calls


def _record(**overrides) -> DeploymentRecord:
    data = {
        "project_id": "p",
        "method": DeployMethod.LOCAL_DOCKER,
        "image": "app:v2",
        "container_name": "app",
        "ports": {"18080": "8000"},
        "env": {"PORT": "8000"},
        "rollback_target": "app:v1",
        "status": DeployStatus.SUCCESS,
    }
    data.update(overrides)
    rec = DeploymentRecord(**data)
    deploy_route._deployment_records[rec.deployment_id] = rec
    return rec


def _rollback(rec: DeploymentRecord) -> dict:
    return asyncio.run(
        deploy_route.rollback(deploy_route.RollbackRequest(deployment_id=rec.deployment_id))
    )


def _run_cmd(calls: list[list[str]]) -> list[str]:
    """stop/rm 을 지나 실제 `docker run` 인자를 고른다."""
    for args in calls:
        if len(args) > 1 and args[1] == "run":
            return args
    raise AssertionError(f"docker run 이 호출되지 않았다: {calls}")


def test_재배포_전에_기존_컨테이너를_교체한다(captured):
    asyncio.run(deploy_route._remove_existing_local_container("app"))

    assert captured == [["docker", "stop", "app"], ["docker", "rm", "app"]]


def test_롤백이_포트_매핑을_재현한다(captured):
    """이 테스트가 이 파일의 존재 이유다."""
    rec = _record()

    result = _rollback(rec)

    assert result["status"] == "ok"
    cmd = _run_cmd(captured)
    assert "-p" in cmd, f"포트 매핑 없이 컨테이너를 띄웠다: {cmd}"
    assert "18080:8000" in cmd, cmd


def test_롤백이_환경변수를_재현한다(captured):
    rec = _record()

    _rollback(rec)

    cmd = _run_cmd(captured)
    assert "-e" in cmd
    assert "PORT=8000" in cmd, cmd


def test_롤백이_이전_이미지로_띄운다(captured):
    rec = _record()

    _rollback(rec)

    cmd = _run_cmd(captured)
    assert cmd[-1] == "app:v1", f"되돌릴 이미지가 아니라 {cmd[-1]} 로 띄웠다"
    assert "app:v2" not in cmd


def test_롤백은_이전_이미지의_포트와_환경을_복원한다(captured):
    """새 릴리스의 실행 계약으로 이전 이미지를 띄우면 복구해도 접속할 수 없다."""
    rec = _record(
        ports={"19000": "9000"},
        env={"PORT": "9000", "NEW_ONLY": "1"},
        rollback_source_deployment_id="previous-v1",
        rollback_ports={"18080": "8000"},
        rollback_env={"PORT": "8000", "LEGACY_SETTING": "yes"},
        rollback_health_check_path="/health",
    )

    result = _rollback(rec)

    cmd = _run_cmd(captured)
    assert "18080:8000" in cmd
    assert "19000:9000" not in cmd
    assert "PORT=8000" in cmd
    assert "LEGACY_SETTING=yes" in cmd
    assert "NEW_ONLY=1" not in cmd
    assert result["ports"] == {"18080": "8000"}


def test_교체_배포가_실패하면_이전_정상_컨테이너를_복원한다(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        if len(args) > 1 and args[1] == "run" and args[-1] == "app:v2":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="new image failed")
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(deploy_route.subprocess, "run", fake_run)
    previous = _record(
        image="app:v1",
        ports={"18080": "8000"},
        env={"PORT": "8000"},
        rollback_eligible=True,
    )
    plan = DeploymentPlan(
        method=DeployMethod.LOCAL_DOCKER,
        action=ActionType.DOCKER_RUN,
        image="app:v2",
        container_name="app",
        ports={"19000": "9000"},
        env={"PORT": "9000"},
    )
    deploy_route._deployment_plans[plan.plan_id] = plan
    monkeypatch.setattr(deploy_route, "_get_continuous_verifier_if_available", lambda: None)

    result = asyncio.run(
        deploy_route.execute_deployment(
            deploy_route.ExecuteRequest(plan_id=plan.plan_id, approved=True)
        )
    )

    assert result["status"] == "failed"
    assert result["restored_previous"] is True
    restore_run = [args for args in calls if len(args) > 1 and args[1] == "run"][-1]
    assert restore_run[-1] == previous.image
    assert "18080:8000" in restore_run
    assert "PORT=8000" in restore_run


def test_성공한_배포는_복원_필드를_포함해_응답한다(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    async def no_health_probe(_plan):  # noqa: ANN001
        return False

    monkeypatch.setattr(deploy_route.subprocess, "run", fake_run)
    monkeypatch.setattr(deploy_route, "_get_continuous_verifier_if_available", lambda: None)
    monkeypatch.setattr(deploy_route, "_verify_rollback_candidate_health", no_health_probe)
    plan = DeploymentPlan(
        method=DeployMethod.LOCAL_DOCKER,
        action=ActionType.DOCKER_RUN,
        image="app:v1",
        container_name="app",
        ports={"18080": "8000"},
    )
    deploy_route._deployment_plans[plan.plan_id] = plan

    result = asyncio.run(
        deploy_route.execute_deployment(
            deploy_route.ExecuteRequest(
                plan_id=plan.plan_id,
                approved=True,
                enable_continuous_verification=False,
            )
        )
    )

    assert result["status"] == "success"
    assert result["restored_previous"] is False
    assert result["restore_stdout"] == ""
    assert result["restore_stderr"] == ""


def test_교체_배포_명령이_예외여도_이전_정상_컨테이너를_복원한다(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        if len(args) > 1 and args[1] == "run" and args[-1] == "app:v2":
            raise subprocess.TimeoutExpired(args, timeout=300)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(deploy_route.subprocess, "run", fake_run)
    _record(image="app:v1", rollback_eligible=True)
    plan = DeploymentPlan(
        method=DeployMethod.LOCAL_DOCKER,
        action=ActionType.DOCKER_RUN,
        image="app:v2",
        container_name="app",
        ports={"19000": "9000"},
    )
    deploy_route._deployment_plans[plan.plan_id] = plan
    monkeypatch.setattr(deploy_route, "_get_continuous_verifier_if_available", lambda: None)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            deploy_route.execute_deployment(
                deploy_route.ExecuteRequest(plan_id=plan.plan_id, approved=True)
            )
        )

    assert exc.value.status_code == 500
    assert "previous container restored" in str(exc.value.detail)
    restore_run = [args for args in calls if len(args) > 1 and args[1] == "run"][-1]
    assert restore_run[-1] == "app:v1"


def test_기존_컨테이너_삭제_중_예외여도_이전_정상_컨테이너를_복원한다(monkeypatch):
    calls: list[list[str]] = []
    remove_attempts = 0

    def fake_run(args, **kwargs):  # noqa: ANN001
        nonlocal remove_attempts
        calls.append(list(args))
        if args[:2] == ["docker", "rm"]:
            remove_attempts += 1
            if remove_attempts == 1:
                raise subprocess.TimeoutExpired(args, timeout=60)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(deploy_route.subprocess, "run", fake_run)
    _record(image="app:v1", rollback_eligible=True)
    plan = DeploymentPlan(
        method=DeployMethod.LOCAL_DOCKER,
        action=ActionType.DOCKER_RUN,
        image="app:v2",
        container_name="app",
        ports={"19000": "9000"},
    )
    deploy_route._deployment_plans[plan.plan_id] = plan
    monkeypatch.setattr(deploy_route, "_get_continuous_verifier_if_available", lambda: None)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            deploy_route.execute_deployment(
                deploy_route.ExecuteRequest(plan_id=plan.plan_id, approved=True)
            )
        )

    assert exc.value.status_code == 500
    assert "previous container restored" in str(exc.value.detail)
    restore_run = [args for args in calls if len(args) > 1 and args[1] == "run"][-1]
    assert restore_run[-1] == "app:v1"


def test_롤백_전에_실패한_릴리스의_지속_감시를_중지한다(captured, monkeypatch):
    rec = _record()
    stopped: list[str] = []

    class _Verifier:
        def list_active(self):
            return [rec.deployment_id]

        async def stop(self, deployment_id: str):
            stopped.append(deployment_id)

    monkeypatch.setattr(deploy_route, "_get_continuous_verifier_if_available", lambda: _Verifier())

    _rollback(rec)

    assert stopped == [rec.deployment_id]


def test_포트_기록이_없으면_경고를_돌려준다(captured):
    """이 필드가 생기기 전의 기록은 롤백해도 밖에서 접속할 수 없다.

    조용히 성공으로 보이면 사용자는 복구됐다고 믿는다.
    """
    rec = _record(ports={})

    result = _rollback(rec)

    assert result["status"] == "ok"
    assert result["warning"], "포트 없이 롤백했는데 경고가 없다"
    assert "접속" in result["warning"]


def test_포트_기록이_있으면_경고가_없다(captured):
    rec = _record()

    result = _rollback(rec)

    assert result["warning"] is None


def test_기록된_포트가_숫자가_아니면_거절한다(captured):
    from fastapi import HTTPException

    rec = _record(ports={"web": "8000"})

    with pytest.raises(HTTPException) as exc:
        _rollback(rec)
    assert exc.value.status_code == 400
    assert "포트" in str(exc.value.detail)


def test_환경변수_이름이_이상하면_거절한다(captured):
    from fastapi import HTTPException

    rec = _record(env={"BAD NAME": "x"})

    with pytest.raises(HTTPException) as exc:
        _rollback(rec)
    assert exc.value.status_code == 400
    assert "환경변수" in str(exc.value.detail)


def test_롤백_대상이_없으면_422(captured):
    from fastapi import HTTPException

    rec = _record(rollback_target=None)

    with pytest.raises(HTTPException) as exc:
        _rollback(rec)
    assert exc.value.status_code == 422
