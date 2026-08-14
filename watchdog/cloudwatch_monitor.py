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


def _recent_stopped_tasks(ecs: Any, target: EcsTarget, window_seconds: int) -> tuple[int, str]:
    """(관측 창 안에서 멈춘 태스크 수, 가장 최근 중단 사유).

    실패해도 예외를 올리지 않는다 — 이건 **보조 정보**다. 이것 때문에 헬스
    수집 전체가 실패하면, 정작 중요한 running/desired 도 못 보게 된다.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    try:
        arns = ecs.list_tasks(
            cluster=target.cluster, serviceName=target.service, desiredStatus="STOPPED",
        ).get("taskArns") or []
        if not arns:
            return 0, ""
        described = ecs.describe_tasks(cluster=target.cluster, tasks=arns[:100])
    except Exception as exc:  # noqa: BLE001
        log.info("중단 태스크 조회 생략: %s", exc)
        return 0, ""

    count = 0
    latest_at: Optional[datetime] = None
    latest_reason = ""
    for task in described.get("tasks") or []:
        stopped_at = task.get("stoppedAt")
        if isinstance(stopped_at, datetime):
            at = stopped_at if stopped_at.tzinfo else stopped_at.replace(tzinfo=timezone.utc)
            if at < cutoff:
                continue
        count += 1
        reason = str(task.get("stoppedReason") or "").strip()
        if reason and (latest_at is None or (
            isinstance(stopped_at, datetime) and stopped_at.replace(
                tzinfo=stopped_at.tzinfo or timezone.utc) > latest_at
        )):
            latest_at = stopped_at if isinstance(stopped_at, datetime) else latest_at
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

    dimensions = [{"Name": "LoadBalancer", "Value": target.load_balancer}]
    if target.target_group:
        dimensions.append({"Name": "TargetGroup", "Value": target.target_group})

    window.requests = _sum_metric(cw, "RequestCount", dimensions, start, end, window_seconds)
    window.errors_5xx = _sum_metric(
        cw, "HTTPCode_Target_5XX_Count", dimensions, start, end, window_seconds,
    )
    window.p95_seconds = _p95_metric(
        cw, "TargetResponseTime", dimensions, start, end, window_seconds,
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
            Period=max(60, period),
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
            Period=max(60, period),
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
