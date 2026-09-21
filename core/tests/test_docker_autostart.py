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
    #: 직전 결과를 그대로 돌려주지는 않는다 — 데몬을 다시 확인한 새 결과다.
    assert first.launched is True and second.launched is False
    assert second.ready is False and "시작 중" in second.message


def test_쿨다운_안이라도_직전에_성공했으면_다시_띄운다(monkeypatch):
    #: 실기기 회귀 — 자동 시작 성공 → 사용자가 Docker 를 끔 → 2분 안에 자동 조치.
    #: 예전엔 직전 "성공" 결과를 그대로 돌려줘 화면은 자동 조치함, 데몬은 죽어 있었다.
    _daemon_seq(monkeypatch, [False, False, True, False, False, True])
    calls = []
    monkeypatch.setattr(da, "_launch", lambda: calls.append(1) or (True, "started"))
    monkeypatch.setattr(da, "app_running", lambda: True)

    first = da.ensure_docker(wait_seconds=10)
    assert first.ready is True
    second = da.ensure_docker(wait_seconds=10)

    assert len(calls) == 2, "성공 뒤 꺼진 Docker 를 다시 띄우지 않았다"
    assert second.ready is True and second.launched is True


def test_쿨다운_안이라도_앱_프로세스가_없으면_다시_띄운다(monkeypatch):
    _daemon_seq(monkeypatch, [False])
    calls = []
    monkeypatch.setattr(da, "_launch", lambda: calls.append(1) or (True, "started"))
    monkeypatch.setattr(da, "app_running", lambda: False)

    da.ensure_docker(wait_seconds=0)
    da.ensure_docker(wait_seconds=0)

    assert len(calls) == 2, "앱이 없는데도 쿨다운을 이유로 띄우지 않았다"


def test_쿨다운_중_데몬이_준비되면_ready_로_돌아온다(monkeypatch):
    _daemon_seq(monkeypatch, [False, False, False, False, True])
    monkeypatch.setattr(da, "_launch", lambda: (True, "started"))
    monkeypatch.setattr(da, "app_running", lambda: True)
    monkeypatch.setattr(da.time, "sleep", lambda _s: None)

    first = da.ensure_docker(wait_seconds=0)   # 띄웠지만 0초 → 미준비
    second = da.ensure_docker(wait_seconds=30)  # 부팅 중 → 기다리기만

    assert first.ready is False
    assert second.ready is True and second.launched is False
    assert "준비됐습니다" in second.message


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


# ---------------------------------------------------------------------------
# 자가 조치 레이어 — /api/docker/ensure (진단판 「자동 조치」 버튼이 부른다)
# ---------------------------------------------------------------------------


def test_docker_ensure_라우트는_결과를_그대로_돌려주고_예외를_내지_않는다(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    from api.routes import health

    monkeypatch.setattr(
        da, "ensure_docker",
        lambda wait_seconds=None: SimpleNamespace(ready=True, attempted=True, launched=True, waited_seconds=12, message="started"),
    )
    r = asyncio.run(health.ensure_docker_route())
    assert r == {"ready": True, "attempted": True, "launched": True, "waited_seconds": 12, "message": "started", "starting": False}

    def boom(wait_seconds=None):
        raise RuntimeError("no docker binary")

    monkeypatch.setattr(da, "ensure_docker", boom)
    r = asyncio.run(health.ensure_docker_route())
    assert r["ready"] is False and "no docker binary" in r["message"]
    assert {"/api/docker/ensure"} <= {route.path for route in health.router.routes}


# ── 실기기 회귀: `open -a Docker` 가 조용히 무시된 뒤 데몬만 기다리다 끝났다 ──


def _fake_clock(monkeypatch, step: float):
    """time.monotonic 이 호출마다 step 씩 흐른다 — 재실행 타이밍을 결정적으로."""
    now = [0.0]

    def mono():
        now[0] += step
        return now[0]

    monkeypatch.setattr(da.time, "monotonic", mono)
    monkeypatch.setattr(da.time, "sleep", lambda _s: None)


def test_macOS에서_open_종료코드가_0이_아니면_실행_실패로_돌려준다(monkeypatch):
    monkeypatch.setattr(da.platform, "system", lambda: "Darwin")

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "Unable to find application named 'Docker'"

    calls = []
    monkeypatch.setattr(da.subprocess, "run", lambda *a, **k: calls.append(a[0]) or Proc())

    launched, how = da._launch()

    assert launched is False
    assert calls == [["open", "-a", "Docker"]]
    assert "Unable to find application" in how and "설치" in how


def test_macOS에서_open_이_성공하면_launched_True(monkeypatch):
    monkeypatch.setattr(da.platform, "system", lambda: "Darwin")

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(da.subprocess, "run", lambda *a, **k: Proc())
    assert da._launch() == (True, "Docker Desktop 실행: open -a Docker")


def test_실행_뒤_앱_프로세스가_사라졌으면_한_번만_다시_띄운다(monkeypatch):
    _daemon_seq(monkeypatch, [False])
    _fake_clock(monkeypatch, step=5.0)  # 폴링마다 5초씩 흐름
    monkeypatch.setattr(da, "app_running", lambda: False)
    launches = []
    monkeypatch.setattr(da, "_launch", lambda: launches.append(1) or (True, "open"))

    r = da.ensure_docker(wait_seconds=60)

    assert r.ready is False and r.launched is True
    assert len(launches) == 2, f"최초 1회 + 재실행 1회여야 하는데 {len(launches)}회 실행"


def test_앱_프로세스가_살아있으면_다시_띄우지_않는다(monkeypatch):
    _daemon_seq(monkeypatch, [False])
    _fake_clock(monkeypatch, step=5.0)
    monkeypatch.setattr(da, "app_running", lambda: True)
    launches = []
    monkeypatch.setattr(da, "_launch", lambda: launches.append(1) or (True, "open"))

    da.ensure_docker(wait_seconds=60)

    assert len(launches) == 1, "부팅 중인 앱을 또 실행했다"


def test_재실행이_준비로_이어지면_ready_True(monkeypatch):
    # 폴링 5회까지 down → 그 뒤 up. 재실행(12초 이후)이 끼어들어도 결과는 ready.
    _daemon_seq(monkeypatch, [False] * 6 + [True])
    _fake_clock(monkeypatch, step=5.0)
    monkeypatch.setattr(da, "app_running", lambda: False)
    monkeypatch.setattr(da, "_launch", lambda: (True, "open"))

    r = da.ensure_docker(wait_seconds=120)

    assert r.ready is True and r.launched is True
    assert "자동 시작했습니다" in r.message


def test_기본_대기_상한은_120초(monkeypatch):
    assert da.DEFAULT_WAIT_SECONDS == 120
