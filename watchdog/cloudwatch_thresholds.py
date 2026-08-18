"""
watchdog/cloudwatch_thresholds.py — ECS/CloudWatch 지표 → 「이상」 판정 (순수 로직)

FR-06-01/02. 배포된 앱이 잘 살아있는지 지켜보고, 이상하면 롤백 제안으로 잇는다.

왜 AWS 호출과 분리했는가
    판정은 **틀려도 예외가 안 난다.** 임계치를 잘못 잡으면 조용히 두 방향으로
    실패한다.
      · 너무 민감 → 멀쩡한 배포에 롤백을 권한다. 사람이 경고를 무시하기
        시작하면 감시 기능 전체가 죽은 것과 같다.
      · 너무 둔감 → 죽은 앱을 정상이라고 말한다.
    자격증명 없이 검사할 수 있게 여기 꺼내 둔다.

특히 조심한 것 — 낮은 트래픽에서의 에러율
    요청 1건 중 1건이 5xx 면 에러율 100% 다. 배포 직후 헬스체크 한 번이
    실패한 것만으로 "에러율 100%" 경보가 나가면 아무도 안 믿는다. 그래서
    최소 요청 수를 넘지 않으면 에러율로는 판정하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class Thresholds:
    """판정 기준. 전부 환경변수로 덮어쓸 수 있다(config 참고)."""

    #: 5xx 비율(0~1). 넘으면 이상.
    error_rate: float = 0.05
    #: 에러율을 **믿을 수 있는** 최소 요청 수. 이보다 적으면 에러율 판정을 건너뛴다.
    #:
    #: 이게 없으면 요청 1건이 실패한 것만으로 "에러율 100%" 가 된다. 배포 직후
    #: 트래픽이 거의 없을 때 항상 그렇게 된다.
    min_requests: int = 20
    #: p95 응답 시간(초). 넘으면 이상.
    p95_seconds: float = 3.0
    #: 이 횟수 이상 연속으로 desired 를 못 채우면 이상.
    #:
    #: 배포 중에는 잠깐 running < desired 가 정상이다. 한 번 보고 바로
    #: 경보하면 **모든 정상 배포마다** 롤백을 권하게 된다.
    unhealthy_polls: int = 3
    #: 이 횟수 이상 태스크가 죽으면 이상(재시작 반복).
    task_restarts: int = 2


@dataclass
class ServiceHealth:
    """ECS 서비스의 현재 상태."""

    running: int = 0
    desired: int = 0
    pending: int = 0
    #: 관측 창 안에서 **크래시로** 멈춘 태스크 수.
    #:
    #: **None 은 "못 읽었다" 이지 "0건" 이 아니다.** ListTasks 가 권한 부족으로
    #: 실패했을 때 0 으로 두면 "재시작 없음, 정상" 이라고 보고하게 되고,
    #: 크래시 루프 감지가 영구히 죽은 채로 조용히 남는다.
    stopped_recently: Optional[int] = None
    #: 가장 최근에 멈춘 이유(있으면). 사용자에게 그대로 보여 준다.
    last_stopped_reason: str = ""

    #: 참고 — 「의도적으로 0대로 내린 상태(desired=0)를 장애로 보고하지 않는다」는
    #: 불변식은 지켜지지만, 그걸 위한 **별도 가드는 두지 않는다.**
    #: `running < desired` 가 desired=0 에서 항상 거짓이라 자동으로 성립한다.
    #: 예전에는 `is_scaled_down` 프로퍼티로 한 번 더 걸렀는데, 변이 시험에서
    #: 그 가드를 지워도 아무 테스트가 깨지지 않아 죽은 코드임이 드러났다.
    #: 불변식 자체는 test_음성대조_0대로_내린_것은_이상이_아니다 가 지킨다.


@dataclass
class MetricWindow:
    """관측 창 동안의 트래픽 지표. 못 읽은 값은 None 으로 둔다.

    **0 과 None 을 구분한다.** 지표를 못 읽은 것(None)을 0 으로 두면
    "에러 0건, 정상" 이라고 말하게 된다 — 감시가 꺼진 걸 정상으로 보고하는 셈.
    """

    requests: Optional[int] = None
    errors_5xx: Optional[int] = None
    p95_seconds: Optional[float] = None
    window_seconds: int = 300

    @property
    def error_rate(self) -> Optional[float]:
        if self.requests is None or self.errors_5xx is None or self.requests <= 0:
            return None
        return self.errors_5xx / self.requests


@dataclass(frozen=True)
class Anomaly:
    """감지한 이상 하나. watchdog 의 알림 계약에 그대로 실린다."""

    alert_type: str
    severity: Severity
    message: str
    metrics: dict = field(default_factory=dict)


def judge(
    health: ServiceHealth,
    metrics: MetricWindow,
    thresholds: Thresholds = Thresholds(),
    consecutive_unhealthy: int = 0,
) -> list[Anomaly]:
    """지표 → 이상 목록. 이상이 없으면 빈 목록.

    Args:
        consecutive_unhealthy: 지금까지 연속으로 desired 를 못 채운 횟수
            (이번 관측 포함). 호출자가 상태로 들고 있는다.
    """
    found: list[Anomaly] = []

    # ── 태스크가 안 뜬다 ────────────────────────────────────────────
    if health.running < health.desired:
        if consecutive_unhealthy >= thresholds.unhealthy_polls:
            found.append(Anomaly(
                alert_type="ecs_tasks_unhealthy",
                severity="critical" if health.running == 0 else "warning",
                message=(
                    f"ECS 태스크가 {consecutive_unhealthy}회 연속으로 목표치를 "
                    f"못 채웠습니다 (실행 {health.running}/{health.desired}"
                    + (f", 대기 {health.pending}" if health.pending else "")
                    + ")."
                    + (f" 최근 중단 사유: {health.last_stopped_reason}"
                       if health.last_stopped_reason else "")
                ),
                metrics={
                    "running": health.running,
                    "desired": health.desired,
                    "pending": health.pending,
                    "consecutive_unhealthy": consecutive_unhealthy,
                },
            ))

    # ── 태스크가 반복해서 죽는다 ────────────────────────────────────
    #
    # 위 검사와 별개다. 태스크가 죽고 곧바로 다시 떠서 running==desired 로
    # 보이는 동안에도 앱은 계속 크래시 중일 수 있다(크래시 루프).
    #: None(못 읽음)이면 건너뛴다. 0 으로 취급하면 "재시작 없음" 이라고
    #: 단언하게 되는데, 실제로는 아무것도 모르는 상태다.
    if (health.stopped_recently or 0) >= thresholds.task_restarts \
            and health.stopped_recently is not None:
        found.append(Anomaly(
            alert_type="ecs_task_restart_loop",
            severity="critical",
            message=(
                f"최근 {metrics.window_seconds // 60}분 동안 태스크가 "
                f"{health.stopped_recently}회 중단됐습니다 — 크래시 반복으로 보입니다."
                + (f" 사유: {health.last_stopped_reason}"
                   if health.last_stopped_reason else "")
            ),
            metrics={
                "stopped_recently": health.stopped_recently,
                "reason": health.last_stopped_reason,
            },
        ))

    # ── 5xx 급증 ────────────────────────────────────────────────────
    rate = metrics.error_rate
    if rate is not None and (metrics.requests or 0) >= thresholds.min_requests:
        if rate > thresholds.error_rate:
            found.append(Anomaly(
                alert_type="http_5xx_spike",
                severity="critical",
                message=(
                    f"5xx 비율이 {rate:.1%} 입니다 (기준 "
                    f"{thresholds.error_rate:.1%}). 최근 "
                    f"{metrics.window_seconds // 60}분 동안 요청 "
                    f"{metrics.requests}건 중 {metrics.errors_5xx}건 실패."
                ),
                metrics={
                    "error_rate": rate,
                    "requests": metrics.requests,
                    "errors_5xx": metrics.errors_5xx,
                },
            ))

    # ── 응답이 느려짐 ───────────────────────────────────────────────
    if metrics.p95_seconds is not None and metrics.p95_seconds > thresholds.p95_seconds:
        found.append(Anomaly(
            alert_type="latency_p95_high",
            severity="warning",
            message=(
                f"응답 시간 p95 가 {metrics.p95_seconds:.2f}초 입니다 "
                f"(기준 {thresholds.p95_seconds:.2f}초)."
            ),
            metrics={"p95_seconds": metrics.p95_seconds},
        ))

    return found


def next_unhealthy_streak(health: ServiceHealth, current_streak: int) -> int:
    """연속 미달 횟수 갱신. 목표를 채운 순간 0 으로 되돌린다.

    되돌리지 않으면 하루에 한 번씩 흔들린 것도 누적돼서, 결국 멀쩡한
    서비스가 임계치를 넘는다.
    """
    if health.running >= health.desired:
        return 0
    return current_streak + 1


#: 참고 — `is_intensive_window`(배포 직후 집중 감시 구간) 는 **삭제했다.**
#: 구현·문서·테스트가 다 있었지만 **호출하는 곳이 한 군데도 없었다.**
#: docstring 은 "이 구간에서는 폴링을 촘촘히 한다" 고 했는데 실제 폴링은
#: ecs_interval_seconds 고정이라, 읽는 사람에게 없는 기능을 있다고 말하는
#: 코드였다. 같은 브랜치에서 `is_scaled_down` 을 지운 이유와 같다.
#: 배포 시각을 데몬에 넘길 수단이 생기면 그때 다시 넣는다.
