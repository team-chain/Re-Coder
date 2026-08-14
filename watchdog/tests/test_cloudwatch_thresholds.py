"""
FR-06-01/02 Watchdog — 이상 판정 (순수 로직)

이 판정은 **틀려도 예외가 안 난다.** 두 방향으로 조용히 실패한다.
  · 너무 민감 → 멀쩡한 배포마다 롤백을 권한다. 사람이 경고를 무시하기
    시작하면 감시 기능 전체가 죽은 것과 같다.
  · 너무 둔감 → 죽은 앱을 정상이라고 말한다.

그래서 "잡는다" 만큼 **"안 잡는다"** 를 같은 무게로 검사한다.
"""
import pytest

from cloudwatch_thresholds import (
    Anomaly,
    MetricWindow,
    ServiceHealth,
    Thresholds,
    is_intensive_window,
    judge,
    next_unhealthy_streak,
)


def _types(anomalies: list[Anomaly]) -> set[str]:
    return {a.alert_type for a in anomalies}


HEALTHY = ServiceHealth(running=2, desired=2)
QUIET = MetricWindow(requests=0, errors_5xx=0, p95_seconds=None)


# ---------------------------------------------------------------------------
# 아무 문제 없을 때는 아무 말도 하지 않는다
# ---------------------------------------------------------------------------


def test_음성대조_정상이면_이상이_없다():
    """이게 깨지면 나머지 검사는 전부 의미가 없다 — 항상 경보하는 감시자다."""
    assert judge(HEALTHY, MetricWindow(requests=1000, errors_5xx=2, p95_seconds=0.4)) == []


def test_음성대조_지표를_못_읽으면_경보하지_않는다():
    """못 읽은 값(None)을 0 이나 문제로 해석하면 안 된다."""
    assert judge(HEALTHY, MetricWindow()) == []


# ---------------------------------------------------------------------------
# 태스크가 안 뜬다
# ---------------------------------------------------------------------------


def test_배포_중_일시적_미달은_경보하지_않는다():
    """배포하면 잠깐 running < desired 가 된다. 한 번 보고 경보하면
    **모든 정상 배포마다** 롤백을 권하게 된다."""
    down = ServiceHealth(running=1, desired=2)
    assert judge(down, QUIET, consecutive_unhealthy=1) == []
    assert judge(down, QUIET, consecutive_unhealthy=2) == []


def test_연속으로_목표치를_못_채우면_경보한다():
    down = ServiceHealth(running=1, desired=2)
    found = judge(down, QUIET, consecutive_unhealthy=3)
    assert "ecs_tasks_unhealthy" in _types(found)


def test_전부_죽었으면_critical():
    """1/2 는 서비스가 살아는 있다. 0/2 는 완전 중단이다 — 등급이 달라야 한다."""
    partial = judge(ServiceHealth(running=1, desired=2), QUIET, consecutive_unhealthy=3)
    none_up = judge(ServiceHealth(running=0, desired=2), QUIET, consecutive_unhealthy=3)

    assert next(a for a in partial if a.alert_type == "ecs_tasks_unhealthy").severity == "warning"
    assert next(a for a in none_up if a.alert_type == "ecs_tasks_unhealthy").severity == "critical"


def test_음성대조_0대로_내린_것은_이상이_아니다():
    """의도적으로 서비스를 멈춘 상태(desired=0)를 장애로 보고하면 안 된다."""
    assert judge(ServiceHealth(running=0, desired=0), QUIET, consecutive_unhealthy=99) == []


def test_연속_횟수는_정상으로_돌아오면_초기화된다():
    """초기화하지 않으면 며칠에 걸쳐 한 번씩 흔들린 것도 누적돼
    결국 멀쩡한 서비스가 임계치를 넘는다."""
    assert next_unhealthy_streak(ServiceHealth(running=1, desired=2), 2) == 3
    assert next_unhealthy_streak(HEALTHY, 5) == 0
    assert next_unhealthy_streak(ServiceHealth(running=0, desired=0), 5) == 0


# ---------------------------------------------------------------------------
# 크래시 반복
# ---------------------------------------------------------------------------


def test_태스크가_반복해서_죽으면_running이_정상이어도_잡는다():
    """죽고 곧바로 다시 떠서 running==desired 로 보이는 동안에도
    앱은 계속 크래시 중일 수 있다."""
    flapping = ServiceHealth(
        running=2, desired=2, stopped_recently=3,
        last_stopped_reason="Essential container in task exited",
    )
    found = judge(flapping, QUIET)
    assert "ecs_task_restart_loop" in _types(found)
    assert "ecs_tasks_unhealthy" not in _types(found), "이건 다른 증상이다"
    assert "Essential container" in found[0].message, "중단 사유를 사용자에게 안 알렸다"


def test_음성대조_한_번_죽은_것은_반복이_아니다():
    assert judge(ServiceHealth(running=2, desired=2, stopped_recently=1), QUIET) == []


# ---------------------------------------------------------------------------
# 5xx — 저트래픽 오탐이 핵심
# ---------------------------------------------------------------------------


def test_5xx_비율이_기준을_넘으면_경보한다():
    found = judge(HEALTHY, MetricWindow(requests=200, errors_5xx=30))
    assert "http_5xx_spike" in _types(found)
    spike = next(a for a in found if a.alert_type == "http_5xx_spike")
    assert "15.0%" in spike.message
    assert "200" in spike.message and "30" in spike.message, "근거 수치가 없다"


def test_요청이_적으면_에러율로_판정하지_않는다():
    """**이 테스트가 이 파일에서 제일 중요하다.**

    요청 1건 중 1건이 5xx 면 에러율 100% 다. 배포 직후 트래픽이 없을 때
    헬스체크 한 번 실패한 것만으로 "에러율 100%" 경보가 나가면 아무도 안
    믿는다. 그 순간부터 감시 기능 전체가 무력해진다.
    """
    assert judge(HEALTHY, MetricWindow(requests=1, errors_5xx=1)) == []
    assert judge(HEALTHY, MetricWindow(requests=19, errors_5xx=19)) == []


def test_최소_요청_수를_넘으면_같은_비율도_잡힌다():
    """위 테스트가 '에러율을 아예 안 본다'는 뜻이 아님을 확인한다."""
    found = judge(HEALTHY, MetricWindow(requests=20, errors_5xx=20))
    assert "http_5xx_spike" in _types(found)


def test_요청이_0이면_0으로_나누지_않는다():
    assert MetricWindow(requests=0, errors_5xx=0).error_rate is None
    assert judge(HEALTHY, MetricWindow(requests=0, errors_5xx=0)) == []


def test_음성대조_기준_이하_에러율은_통과시킨다():
    """항상 잡으면 위 검사들은 아무것도 증명하지 못한다."""
    assert judge(HEALTHY, MetricWindow(requests=1000, errors_5xx=10)) == []   # 1%


# ---------------------------------------------------------------------------
# 응답 시간
# ---------------------------------------------------------------------------


def test_p95가_기준을_넘으면_경보한다():
    found = judge(HEALTHY, MetricWindow(requests=100, errors_5xx=0, p95_seconds=5.0))
    assert "latency_p95_high" in _types(found)


def test_음성대조_빠르면_경보하지_않는다():
    assert judge(HEALTHY, MetricWindow(requests=100, errors_5xx=0, p95_seconds=0.2)) == []


def test_p95를_못_읽었으면_건너뛴다():
    """요청이 없으면 응답 시간도 없다. 0 초로 채우면 '아주 빠름'이 된다."""
    assert judge(HEALTHY, MetricWindow(requests=0, errors_5xx=0, p95_seconds=None)) == []


# ---------------------------------------------------------------------------
# 임계치 조정
# ---------------------------------------------------------------------------


def test_임계치를_바꾸면_판정도_바뀐다():
    """임계치가 실제로 쓰이는지 — 상수를 무시하고 하드코딩했으면 여기서 걸린다."""
    metrics = MetricWindow(requests=100, errors_5xx=3)     # 3%
    assert judge(HEALTHY, metrics) == []
    strict = Thresholds(error_rate=0.01)
    assert "http_5xx_spike" in _types(judge(HEALTHY, metrics, strict))


def test_여러_이상이_동시에_잡힌다():
    """하나 찾고 멈추면 사용자는 문제를 하나씩만 보게 된다."""
    bad = ServiceHealth(running=0, desired=2, stopped_recently=5)
    found = judge(bad, MetricWindow(requests=100, errors_5xx=50, p95_seconds=9.0),
                  consecutive_unhealthy=5)
    assert _types(found) == {
        "ecs_tasks_unhealthy", "ecs_task_restart_loop", "http_5xx_spike", "latency_p95_high",
    }


# ---------------------------------------------------------------------------
# 배포 직후 집중 감시
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed, expected",
    [(0, True), (60, True), (300, True), (301, False), (3600, False), (None, False), (-5, False)],
)
def test_배포_직후_5분이_집중_감시_구간(elapsed, expected):
    """문제는 대개 배포 직후에 드러나고, 그때 빨리 잡아야 롤백 제안이 의미가 있다."""
    assert is_intensive_window(elapsed) is expected
