"""컨테이너 교체의 안전 장치 — 회차4 E2E 통합 검증 (코드리뷰 대응).

여기서 지키는 세 가지는 모두 **평소에는 드러나지 않는다.** 정상 흐름에서는
아무 일도 없고, 코어가 재시작됐거나 요청이 겹치거나 롤백이 성공한 뒤에야
결과가 갈린다. 그래서 사람 눈으로는 회귀를 잡을 수 없고 테스트로 고정한다.

  1. 기록에 없는 컨테이너도 파괴 전에 붙잡는다
     코어 재시작 후 `_deployment_records` 는 비어 있지만 컨테이너는 살아 있다.
     기록이 없다는 건 "되돌릴 것이 없다" 가 아니라 "우리가 모른다" 일 뿐인데,
     그대로 지우고 새 run 이 실패하면 서비스가 통째로 사라진다.

  2. 같은 컨테이너를 노리는 교체를 직렬화한다
     두 배포가 겹치면 stop/rm/run 이 서로 끼어든다. 한쪽이 띄운 뒤 다른 쪽이
     이름 충돌로 실패하면, 그 실패 경로의 복구가 **이긴 쪽 컨테이너를 지우고**
     옛 릴리스를 되살린다 — 성공 응답과 실제 상태가 어긋난다.

  3. 롤백 성공 후 감시를 다시 건다
     이전 릴리스 감시는 교체 때 꺼졌고 실패 릴리스 감시도 롤백 때 끈다.
     여기서 시작하지 않으면 되살아난 서비스가 영구히 감시 밖에 남는다.
"""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

import api.routes.deploy as deploy_route
from schemas import (
    ActionType,
    DeploymentPlan,
    DeploymentRecord,
    DeployMethod,
    DeployStatus,
)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    deploy_route._deployment_records.clear()
    deploy_route._deployment_plans.clear()
    deploy_route._container_locks.clear()
    monkeypatch.setattr(deploy_route, "_get_continuous_verifier_if_available", lambda: None)

    async def healthy(_ports, _path):
        return True

    monkeypatch.setattr(deploy_route, "_probe_local_http_health", healthy)
    yield
    deploy_route._deployment_records.clear()
    deploy_route._deployment_plans.clear()
    deploy_route._container_locks.clear()


def _plan(image: str = "app:v2", container: str = "app") -> DeploymentPlan:
    plan = DeploymentPlan(
        method=DeployMethod.LOCAL_DOCKER,
        action=ActionType.DOCKER_RUN,
        image=image,
        container_name=container,
        ports={"19000": "9000"},
    )
    deploy_route._deployment_plans[plan.plan_id] = plan
    return plan


def _execute(plan: DeploymentPlan) -> dict:
    return asyncio.run(
        deploy_route.execute_deployment(
            deploy_route.ExecuteRequest(plan_id=plan.plan_id, approved=True)
        )
    )


#: `docker inspect --format {{json .}}` 가 돌려주는 모양의 최소 응답.
_INSPECT_JSON = json.dumps({
    "State": {"Running": True},
    "Image": "sha256:legacyid",
    "Config": {"Image": "legacy:v9", "Env": ["PORT=8000", "PATH=/usr/bin", "BAD NAME=x"]},
    "HostConfig": {"PortBindings": {"8000/tcp": [{"HostIp": "", "HostPort": "18080"}]}},
})


# ---------------------------------------------------------------------------
# 1. 기록에 없는 컨테이너 보존
# ---------------------------------------------------------------------------


def test_기록에_없는_컨테이너도_교체_전에_붙잡아_복구한다(monkeypatch):
    """코어 재시작 뒤 재배포가 실패해도 이전 서비스가 사라지면 안 된다."""
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args, 0, stdout=_INSPECT_JSON, stderr="")
        if len(args) > 1 and args[1] == "run" and args[-1] == "app:v2":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="new image failed")
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(deploy_route.subprocess, "run", fake_run)
    # 배포 기록은 비어 있다 — 코어가 방금 재시작된 상황.
    assert not deploy_route._deployment_records

    result = _execute(_plan())

    assert result["status"] == "failed"
    assert result["restored_previous"] is True, (
        "기록이 없다는 이유로 살아 있던 컨테이너를 그냥 잃었다"
    )
    restore_run = [a for a in calls if len(a) > 1 and a[1] == "run"][-1]
    assert restore_run[-1] == "sha256:legacyid", restore_run
    assert "18080:8000" in restore_run, "붙잡은 포트 매핑이 복구에 쓰이지 않았다"


def test_붙잡은_환경변수에서_이름이_이상한_항목은_버린다(monkeypatch):
    """거르지 않으면 복구가 인자 검증에서 막혀 아예 되살리지 못한다."""
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args, 0, stdout=_INSPECT_JSON, stderr="")
        if len(args) > 1 and args[1] == "run" and args[-1] == "app:v2":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="fail")
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(deploy_route.subprocess, "run", fake_run)

    _execute(_plan())

    restore_run = [a for a in calls if len(a) > 1 and a[1] == "run"][-1]
    joined = " ".join(restore_run)
    assert "PORT=8000" in joined
    assert "BAD NAME" not in joined, f"이름이 이상한 환경변수가 그대로 나갔다: {restore_run}"


def test_종료된_컨테이너는_되살리지_않는다(monkeypatch):
    """이름만 남은 종료 컨테이너를 "돌던 서비스" 로 오인하면 안 된다.

    사람이 일부러 내려둔 것을 새 배포 실패를 계기로 되살려 놓고 복구했다고
    보고하게 된다.
    """
    exited = json.dumps({
        "State": {"Running": False},
        "Image": "sha256:oldid",
        "Config": {"Image": "legacy:v9", "Env": []},
        "HostConfig": {"PortBindings": {}},
    })

    def fake_run(args, **kwargs):  # noqa: ANN001
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args, 0, stdout=exited, stderr="")
        if len(args) > 1 and args[1] == "run" and args[-1] == "app:v2":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="fail")
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(deploy_route.subprocess, "run", fake_run)

    result = _execute(_plan())

    assert result["status"] == "failed"
    assert result["restored_previous"] is False, "내려가 있던 컨테이너를 되살렸다"


def test_그런_컨테이너가_없으면_붙잡지_않는다(monkeypatch):
    """첫 배포다. inspect 가 실패해도 배포 자체는 정상 진행돼야 한다."""
    def fake_run(args, **kwargs):  # noqa: ANN001
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="No such object")
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(deploy_route.subprocess, "run", fake_run)

    result = _execute(_plan())

    assert result["status"] == "success"
    assert result["restored_previous"] is False


# ---------------------------------------------------------------------------
# 2. 같은 컨테이너 교체 직렬화
# ---------------------------------------------------------------------------


def test_같은_컨테이너는_같은_락을_공유하고_다른_컨테이너는_아니다():
    async def scenario():
        a1 = await deploy_route._lock_for_container("app")
        a2 = await deploy_route._lock_for_container("app")
        b = await deploy_route._lock_for_container("worker")
        return a1, a2, b

    a1, a2, b = asyncio.run(scenario())
    assert a1 is a2, "같은 컨테이너인데 락이 갈렸다 — 직렬화가 되지 않는다"
    assert a1 is not b, "다른 컨테이너까지 서로 막으면 불필요하게 느려진다"


def test_락이_잡혀_있으면_docker_를_건드리기_전에_기다린다(monkeypatch):
    """직렬화의 핵심 — 파괴적 구간에 진입하기 **전에** 막혀야 한다.

    락을 잡은 채로 배포를 시작시키고, 그 사이 docker 명령이 하나도 나가지
    않는지 본다. 나간다면 두 요청의 stop/rm/run 이 섞일 수 있다는 뜻이다.
    """
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(deploy_route.subprocess, "run", fake_run)
    plan = _plan()

    async def scenario():
        lock = await deploy_route._lock_for_container("app")
        await lock.acquire()
        task = asyncio.create_task(
            deploy_route.execute_deployment(
                deploy_route.ExecuteRequest(plan_id=plan.plan_id, approved=True)
            )
        )
        await asyncio.sleep(0.05)
        blocked_calls = list(calls)
        blocked = not task.done()
        lock.release()
        result = await task
        return blocked, blocked_calls, result

    blocked, blocked_calls, result = asyncio.run(scenario())

    assert blocked, "락이 잡혀 있는데 배포가 그대로 진행됐다"
    assert blocked_calls == [], f"락을 얻기 전에 docker 를 건드렸다: {blocked_calls}"
    assert result["status"] == "success", "락을 놓은 뒤에는 정상 완료돼야 한다"


def test_락은_실패한_배포_뒤에도_풀린다(monkeypatch):
    """풀리지 않으면 그 컨테이너로 가는 모든 후속 요청이 영원히 멈춘다."""
    def fake_run(args, **kwargs):  # noqa: ANN001
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="none")
        if len(args) > 1 and args[1] == "run":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(deploy_route.subprocess, "run", fake_run)

    _execute(_plan())

    async def check():
        lock = await deploy_route._lock_for_container("app")
        return lock.locked()

    assert asyncio.run(check()) is False, "실패한 배포가 락을 잡은 채 끝났다"


# ---------------------------------------------------------------------------
# 3. 롤백 후 감시 재시작
# ---------------------------------------------------------------------------


def test_롤백에_성공하면_되살아난_릴리스의_감시를_다시_건다(monkeypatch):
    started: list[str] = []

    class _Verifier:
        def list_active(self):
            return []

        async def stop(self, deployment_id: str):
            pass

        async def start(self, deployment_id: str, **kwargs):
            started.append(deployment_id)
            return {"deployment_id": deployment_id, "status": "started"}

    monkeypatch.setattr(deploy_route, "_get_continuous_verifier_if_available", lambda: _Verifier())
    monkeypatch.setattr(
        deploy_route.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="ok", stderr=""),
    )

    previous = DeploymentRecord(
        project_id="p", method=DeployMethod.LOCAL_DOCKER, image="app:v1",
        container_name="app", ports={"18080": "8000"}, status=DeployStatus.SUCCESS,
    )
    deploy_route._deployment_records[previous.deployment_id] = previous

    failed = DeploymentRecord(
        project_id="p", method=DeployMethod.LOCAL_DOCKER, image="app:v2",
        container_name="app", ports={"18080": "8000"},
        rollback_target="app:v1",
        rollback_source_deployment_id=previous.deployment_id,
        rollback_ports={"18080": "8000"},
        status=DeployStatus.SUCCESS,
    )
    deploy_route._deployment_records[failed.deployment_id] = failed

    result = asyncio.run(
        deploy_route.rollback(deploy_route.RollbackRequest(deployment_id=failed.deployment_id))
    )

    assert result["status"] == "ok"
    assert result["verification_resumed"] is True
    assert started == [previous.deployment_id], (
        f"되살아난 릴리스가 감시 밖에 남았다: {started}"
    )
