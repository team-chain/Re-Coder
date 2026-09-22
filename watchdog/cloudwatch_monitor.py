"""
watchdog/cloudwatch_monitor.py — ECS/CloudWatch 지표 수집

FR-06-01/02. `docker_monitor.py` 가 로컬 도커를 보듯이, 이 모듈은 **배포된 ECS
서비스**를 본다. 상태는 ECS API 에서, 트래픽 지표는 CloudWatch 에서 읽는다.

읽는 것
  · 헬스   — ECS DescribeServices 의 running/desired/pending, 최근 중단 태스크
  · 에러율 — ALB 의 HTTPCode_Target_5XX_Count / RequestCount
  · 응답속도 — ALB 의 TargetResponseTime p95

판정은 여기서 하지 않는다. `cloudwatch_thresholds.judge()` 가 한다 — 임계치는
자격증명 없이 검사할 수 있어야 해서 분리했다.

설계 원칙: **못 읽은 값을 0 으로 채우지 않는다.**
    지표 조회가 실패했는데 0 으로 두면 "에러 0건, 정상" 이라고 보고하게 된다.
    감시가 꺼진 것을 정상으로 말하는 셈이라, 못 읽었으면 None 으로 남긴다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cloudwatch_thresholds import MetricWindow, ServiceHealth

log = logging.getLogger(__name__)

#: list_tasks 페이지 상한. 100개/페이지 → 최대 500건까지 본다.
_MAX_TASK_PAGES = 5


def _valid_period(seconds: int) -> int:
    """CloudWatch 가 받아 주는 Period(60의 배수, 최소 60)."""
    return max(60, (int(seconds) // 60) * 60)


class CloudWatchUnavailableError(RuntimeError):
    """AWS 를 못 읽는다 — 호출자가 재시도·알림 정책을 정한다.

    `DockerUnavailableError` 와 같은 자리를 차지한다. 감시 실패를 **정상**과
    구분하기 위해 예외로 올린다.
    """


@dataclass(frozen=True)
class EcsTarget:
    """어느 서비스를 보는가."""

    cluster: str
    service: str
    region: str
    #: ALB 지표용. 없으면 트래픽 지표는 건너뛰고 헬스만 본다.
    load_balancer: str = ""
    target_group: str = ""


def _clients(target: EcsTarget, session: Any = None) -> tuple[Any, Any]:
    """(ecs, cloudwatch). session 을 주면 그 자격증명을 쓴다(BYO)."""
    if not target.region.strip():
        #: 리전이 비면 botocore 가 `Invalid endpoint: https://ecs..amazonaws.com`
        #: 을 낸다. 그 메시지로는 무엇을 설정해야 하는지 알 수 없어서, 설정
        #: 실수를 설정 실수라고 말해 준다. 감시가 안 도는 이유가 로그에
        #: 안 보이면 사람들은 감시가 도는 줄 안다.
        raise CloudWatchUnavailableError(
            "AWS 리전이 설정되지 않았습니다 — RECODER_WATCHDOG_AWS_REGION "
            "(또는 AWS_REGION) 을 설정하세요. 지금은 ECS 감시가 동작하지 않습니다."
        )

    try:
        import boto3  # type: ignore
    except ImportError as exc:  # pragma: no cover - 배포 환경엔 항상 있다
        raise CloudWatchUnavailableError(
            "boto3 가 설치되어 있지 않습니다. 'pip install boto3' 후 다시 시도하세요."
        ) from exc

    sess = session or boto3.session.Session(region_name=target.region)
    return (
        sess.client("ecs", region_name=target.region),
        sess.client("cloudwatch", region_name=target.region),
    )


# ---------------------------------------------------------------------------
# 헬스 (ECS)
# ---------------------------------------------------------------------------


def collect_service_health(
    target: EcsTarget, *, window_seconds: int = 300, session: Any = None,
) -> ServiceHealth:
    """ECS 서비스의 현재 상태.

    최근 중단 태스크는 `ListTasks(desiredStatus=STOPPED)` → `DescribeTasks` 로
    센다. 관측 창 밖에서 멈춘 것은 제외한다 — 어제 한 번 죽은 것까지 세면
    오늘 배포가 계속 이상으로 잡힌다.
    """
    ecs, _ = _clients(target, session)

    try:
        resp = ecs.describe_services(cluster=target.cluster, services=[target.service])
    except Exception as exc:  # noqa: BLE001
        raise CloudWatchUnavailableError(f"ECS 서비스 조회 실패: {exc}") from exc

    services = resp.get("services") or []
    if not services:
        raise CloudWatchUnavailableError(
            f"서비스를 찾을 수 없습니다: {target.cluster}/{target.service}"
        )
    svc = services[0]

    health = ServiceHealth(
        running=int(svc.get("runningCount") or 0),
        desired=int(svc.get("desiredCount") or 0),
        pending=int(svc.get("pendingCount") or 0),
    )

    stopped, reason = _recent_stopped_tasks(ecs, target, window_seconds)
    health.stopped_recently = stopped
    health.last_stopped_reason = reason
    return health


#: **의도적으로** 멈춘 태스크의 사유. 크래시로 세지 않는다.
#:
#: 이걸 안 거르면 `desiredCount=2` 서비스를 **정상 배포할 때마다** 옛 태스크
#: 2개가 멈추면서 task_restarts(기본 2)를 채워 critical 「크래시 반복」이
#: 나간다. 멀쩡한 배포마다 경보가 나면 사람이 경고를 무시하기 시작하고,
#: 그 시점에 감시 기능은 죽은 것과 같다.
#:
#: 화이트리스트가 아니라 **블랙리스트**인 이유: 모르는 사유는 크래시로
#: 센다. 새로운 실패 방식을 놓치는 쪽이 더 위험하다.
_DELIBERATE_STOP_MARKERS = (
    "scaling activity initiated by deployment",
    "task stopped by user",
    "stopped by user",
    "service scheduler",
    "deployment",
)


def _is_deliberate_stop(reason: str) -> bool:
    low = reason.strip().lower()
    if not low:
        return False
    return any(marker in low for marker in _DELIBERATE_STOP_MARKERS)


def _recent_stopped_tasks(
    ecs: Any, target: EcsTarget, window_seconds: int,
) -> tuple[Optional[int], str]:
    """(관측 창 안에서 크래시로 멈춘 태스크 수, 가장 최근 중단 사유).

    **못 읽으면 None 이다. 0 이 아니다.** 예전에는 예외를 삼키고 0 을
    돌려줬는데, 그러면 `ecs:ListTasks` 권한이 없는 인스턴스에서 크래시 루프
    감지가 영구히 죽고도 스냅샷에는 "중단 0건" 이 남는다 — 감시가 꺼진 것을
    정상으로 보고하는 셈이다.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)

    arns: list = []
    try:
        token: Optional[str] = None
        #: 페이지네이션. 크래시 루프가 심할수록 STOPPED 태스크가 쌓이는데,
        #: 첫 100개만 보면 그게 전부 창 밖일 수 있어 **장애가 심해질수록
        #: 감지가 조용해지는** 역전이 생긴다.
        for _ in range(_MAX_TASK_PAGES):
            kwargs = dict(
                cluster=target.cluster, serviceName=target.service,
                desiredStatus="STOPPED",
            )
            if token:
                kwargs["nextToken"] = token
            page = ecs.list_tasks(**kwargs)
            arns.extend(page.get("taskArns") or [])
            token = page.get("nextToken")
            if not token:
                break
        else:
            log.warning(
                "중단 태스크가 %d페이지를 넘습니다 — 최근 %d건까지만 셉니다.",
                _MAX_TASK_PAGES, len(arns),
            )

        if not arns:
            return 0, ""

        tasks: list = []
        for start in range(0, len(arns), 100):     # DescribeTasks 는 100개 상한
            tasks.extend(
                ecs.describe_tasks(
                    cluster=target.cluster, tasks=arns[start:start + 100],
                ).get("tasks") or []
            )
    except Exception as exc:  # noqa: BLE001
        #: 여기서 0 을 돌려주면 "재시작 없음" 이라고 단언하게 된다.
        log.warning("중단 태스크를 읽지 못했습니다(크래시 루프 감지 불가): %s", exc)
        return None, ""

    count = 0
    latest_at: Optional[datetime] = None
    latest_reason = ""
    for task in tasks:
        stopped_at = task.get("stoppedAt")
        if not isinstance(stopped_at, datetime):
            #: `ListTasks(desiredStatus=STOPPED)` 는 **아직 draining 중**이라
            #: stoppedAt 이 없는 태스크도 돌려준다. 예전에는 그것들이 창
            #: 필터를 그냥 통과해 배포 중 오탐을 키웠다. 멈추지 않은 것은
            #: 세지 않는다.
            continue
        at = stopped_at if stopped_at.tzinfo else stopped_at.replace(tzinfo=timezone.utc)
        if at < cutoff:
            continue

        reason = str(task.get("stoppedReason") or "").strip()
        if _is_deliberate_stop(reason):
            continue

        count += 1
        if latest_at is None or at > latest_at:
            latest_at = at
            latest_reason = reason
    return count, latest_reason


# ---------------------------------------------------------------------------
# 트래픽 (CloudWatch / ALB)
# ---------------------------------------------------------------------------


def collect_traffic_metrics(
    target: EcsTarget, *, window_seconds: int = 300, session: Any = None,
) -> MetricWindow:
    """ALB 지표 창. ALB 정보가 없으면 값이 전부 None 인 창을 돌려준다.

    ALB 가 없는 배포(내부 서비스·배치)도 정상이므로 예외로 올리지 않는다.
    다만 **0 으로 채우지도 않는다** — 못 본 것과 문제없는 것은 다르다.
    """
    window = MetricWindow(window_seconds=window_seconds)
    if not target.load_balancer:
        return window

    _, cw = _clients(target, session)
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=window_seconds)

    #: 차원을 둘로 나눈다. **이걸 뭉뚱그리면 최악의 장애를 놓친다.**
    #:
    #: 대상 그룹에 healthy 타깃이 하나도 없으면 ALB 가 **자기가** 503 을 낸다.
    #: 그 503 은 HTTPCode_ELB_5XX_Count 에만 잡히고 Target_5XX 에는 안 잡힌다.
    #: 그리고 TargetGroup 차원으로 RequestCount 를 읽으면 그것도 0 이다.
    #: 예전 구현은 Target_5XX 하나만 TargetGroup 차원으로 읽어서, 전면 장애가
    #: "요청 0건, 에러 0건" 으로 보였다 — 알림이 하나도 안 나갔다.
    lb_only = [{"Name": "LoadBalancer", "Value": target.load_balancer}]
    per_target = list(lb_only)
    if target.target_group:
        per_target.append({"Name": "TargetGroup", "Value": target.target_group})

    #: 요청 수는 **ALB 가 받은 전체**로 센다. 타깃이 다 죽어도 사용자는
    #: 요청을 보내고 있고, 그게 에러율의 분모여야 한다.
    window.requests = _sum_metric(cw, "RequestCount", lb_only, start, end, window_seconds)

    target_5xx = _sum_metric(
        cw, "HTTPCode_Target_5XX_Count", per_target, start, end, window_seconds,
    )
    elb_5xx = _sum_metric(
        cw, "HTTPCode_ELB_5XX_Count", lb_only, start, end, window_seconds,
    )
    #: 하나라도 못 읽었으면 합계도 모르는 값이다 — 0 으로 채우지 않는다.
    if target_5xx is None and elb_5xx is None:
        window.errors_5xx = None
    else:
        window.errors_5xx = (target_5xx or 0) + (elb_5xx or 0)

    window.p95_seconds = _p95_metric(
        cw, "TargetResponseTime", per_target, start, end, window_seconds,
    )
    return window


def _sum_metric(
    cw: Any, name: str, dimensions: list, start: datetime, end: datetime, period: int,
) -> Optional[int]:
    """합계 지표. 못 읽으면 None (0 이 아니다)."""
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/ApplicationELB",
            MetricName=name,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            #: Period 는 60의 배수여야 한다. 아니면 CloudWatch 가 요청을
            #: 거부하고, 그 예외가 여기서 None 으로 삼켜져 지표가 조용히
            #: 사라진다(ECS_WINDOW_SECONDS=301 같은 값에서 실제로 그렇다).
            Period=_valid_period(period),
            Statistics=["Sum"],
        )
    except Exception as exc:  # noqa: BLE001
        log.info("지표 %s 조회 실패: %s", name, exc)
        return None

    points = resp.get("Datapoints") or []
    if not points:
        #: 데이터가 없다 = 그 창에 요청이 없었다. CloudWatch 는 0 을 점으로
        #: 남기지 않는다. 이건 "못 읽음" 이 아니라 **실제로 0** 이다.
        return 0
    return int(sum(float(p.get("Sum") or 0) for p in points))


def _p95_metric(
    cw: Any, name: str, dimensions: list, start: datetime, end: datetime, period: int,
) -> Optional[float]:
    """p95 지표. ExtendedStatistics 를 쓴다 — Statistics 로는 백분위를 못 얻는다."""
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/ApplicationELB",
            MetricName=name,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            Period=_valid_period(period),
            ExtendedStatistics=["p95"],
        )
    except Exception as exc:  # noqa: BLE001
        log.info("지표 %s(p95) 조회 실패: %s", name, exc)
        return None

    points = resp.get("Datapoints") or []
    values = [
        float(p["ExtendedStatistics"]["p95"])
        for p in points
        if isinstance(p.get("ExtendedStatistics"), dict)
        and p["ExtendedStatistics"].get("p95") is not None
    ]
    if not values:
        #: 요청이 없으면 응답 시간도 없다. 0 초로 두면 "아주 빠름" 이 되므로
        #: None 으로 남긴다 — 판정에서 건너뛴다.
        return None
    return max(values)
