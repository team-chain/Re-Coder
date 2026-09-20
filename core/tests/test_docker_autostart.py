"""Docker Desktop 자동 기동 계약 테스트.

핵심 계약:
- 데몬이 살아 있으면 아무것도 실행하지 않는다.
- 꺼져 있으면 백그라운드 실행을 시도하고 준비까지 기다린다.
- 실패해도 예외 없이 결과 객체로 돌아온다 — 호출자는 기존 fail-closed
  경로를 그대로 탄다 (통과 위장 금지).
- 쿨다운 안에서는 재실행하지 않는다 (화면 폴링이 앱을 여러 번 띄우는 것 방지).
"""
from __future__ import annotations

import pytest

import docker_autostart as da
from agents import ecs_build
from agents.ecs_build import BuildError, ensure_docker_available


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    da.reset_for_tests()
    monkeypatch.setattr(da, "_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.delenv(da.ENV_AUTOSTART, raising=False)
    monkeypatch.delenv(da.ENV_WAIT_SECONDS, raising=False)
    yield
    da.reset_for_tests()


def _daemon_seq(monkeypatch, states: list[bool]):
    """daemon_up 이 states 를 순서대로 돌려주다가 마지막 값을 유지한다."""
    it = iter(states)
    last = states[-1]

    def fake(timeout: float = 5.0) -> bool:
        nonlocal last
        try:
            last = next(it)
        except StopIteration:
            pass
        return last

    monkeypatch.setattr(da, "daemon_up", fake)


def test_데몬이_살아있으면_아무것도_실행하지_않는다(monkeypatch):
    _daemon_seq(monkeypatch, [True])
    launched = []
    monkeypatch.setattr(da, "_launch", lambda: launched.append(1) or (True, "x"))

    r = da.ensure_docker()

    assert r.ready is True and r.attempted is False
    assert launched == [], "데몬이 떠 있는데 Docker Desktop 을 또 실행했다"


def test_환경변수로_끄면_시도하지_않고_사유를_말한다(monkeypatch):
    _daemon_seq(monkeypatch, [False])
    monkeypatch.setenv(da.ENV_AUTOSTART, "0")
    monkeypatch.setattr(da, "_launch", lambda: (_ for _ in ()).throw(AssertionError("실행 금지")))

    r = da.ensure_docker()

    assert r.ready is False and r.attempted is False
    assert da.ENV_AUTOSTART in r.message


def test_실행파일을_못찾으면_실행실패_사유로_끝난다(monkeypatch):
    _daemon_seq(monkeypatch, [False])
    monkeypatch.setattr(da, "_launch", lambda: (False, "실행 파일 없음"))

    r = da.ensure_docker()

    assert r.attempted is True and r.launched is False and r.ready is False
    assert "실행 파일 없음" in r.message


def test_실행_후_데몬이_뜨면_ready(monkeypatch):
    # 최초 확인 down → 락 안 재확인 down → 폴링 1회차 down → 2회차 up
    _daemon_seq(monkeypatch, [False, False, False, True])
    monkeypatch.setattr(da, "_launch", lambda: (True, "started"))

    r = da.ensure_docker(wait_seconds=5)

    assert r.attempted and r.launched and r.ready
    assert "자동 시작" in r.message


def test_시간내_준비_안되면_ready_False_와_안내(monkeypatch):
    _daemon_seq(monkeypatch, [False])
    monkeypatch.setattr(da, "_launch", lambda: (True, "started"))

    r = da.ensure_docker(wait_seconds=0)

    assert r.attempted and r.launched and r.ready is False
    assert "준비되지 않았습니다" in r.message


def test_쿨다운_안에서는_재실행하지_않는다(monkeypatch):
    _daemon_seq(monkeypatch, [False])
    calls = []
    monkeypatch.setattr(da, "_launch", lambda: calls.append(1) or (True, "started"))

    first = da.ensure_docker(wait_seconds=0)
    second = da.ensure_docker(wait_seconds=0)

    assert len(calls) == 1, "실패 직후 재호출에서 Docker Desktop 을 또 실행했다"
    assert second is first, "쿨다운 중에는 직전 결과를 그대로 돌려줘야 한다"


# ── ensure_docker_available 통합 ──────────────────────────────────────────

def _runner_seq(results):
    it = iter(results)

    def runner(cmd, timeout=None):
        return next(it)

    return runner


def test_빌드_관문은_자동시작_성공_후_원래_작업을_계속한다(monkeypatch):
    monkeypatch.setattr(ecs_build.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        da, "ensure_docker",
        lambda wait_seconds=None: da.AutostartResult(True, True, True, 3, "자동 시작 완료"),
    )
    runner = _runner_seq([(1, "", "daemon down"), (0, "27.0.1", "")])

    version = ensure_docker_available(runner=runner)

    assert version == "27.0.1"


def test_빌드_관문은_자동시작_실패시_시도내역을_담아_실패한다(monkeypatch):
    monkeypatch.setattr(ecs_build.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        da, "ensure_docker",
        lambda wait_seconds=None: da.AutostartResult(True, True, False, 75, "75초 안에 준비되지 않았습니다"),
    )
    runner = _runner_seq([(1, "", "daemon down")])

    with pytest.raises(BuildError) as exc:
        ensure_docker_available(runner=runner)

    text = str(exc.value) + (getattr(exc.value, "remedy", "") or "") + (getattr(exc.value, "detail", "") or "")
    assert "자동 시작" in text, "무엇을 시도했는지 사용자에게 말하지 않는다"


# ── 스캔 라우트 통합 — 데몬이 없으면 미검증(not_run)으로 ──────────────────

def test_trivy_스캔은_데몬_부재시_자동시작을_시도하고_미검증으로_남는다(monkeypatch):
    import asyncio
    from api.routes import deploy as deploy_route

    monkeypatch.setattr(
        da, "ensure_docker",
        lambda wait_seconds=None: da.AutostartResult(True, True, False, 75, "자동 시작 실패 사유"),
    )
    monkeypatch.setattr(deploy_route, "_get_infra_agent", lambda: object())

    result = asyncio.run(deploy_route._execute_scan("trivy", "", "app:latest"))

    assert result["status"] == "not_run", "데몬이 없는데 통과/실패로 위장했다"
    assert "자동 시작 실패 사유" in result["message"], "무엇을 시도했는지 화면에 전달되지 않는다"
    assert result["findings"] == []
