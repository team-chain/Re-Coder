"""
FR-06-01/02 Watchdog — ECS/CloudWatch 수집

판정(test_cloudwatch_thresholds.py)이 기준을 지키고, 여기서는 **AWS 응답을
올바르게 읽는지**를 본다.

ECS 는 moto 로 진짜 서비스를 만들어 검사한다 — 응답 모양을 내가 상상해서
쓰면, 그 상상이 틀렸을 때 테스트는 통과하고 실제로만 깨진다.

CloudWatch 는 스텁을 쓴다. moto 의 `get_metric_statistics` 는
ExtendedStatistics(p95)를 온전히 지원하지 않아서, 그걸로는 정작 검사하려는
p95 경로를 못 태운다. 대신 **AWS 문서의 응답 모양을 그대로** 흉내내고,
호출 인자(Namespace·Period·Statistics)까지 함께 검사한다.
"""
from datetime import datetime, timedelta, timezone

import pytest

import cloudwatch_monitor as M
from cloudwatch_monitor import CloudWatchUnavailableError, EcsTarget

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

REGION = "us-east-1"
TARGET = EcsTarget(cluster="recoder-cluster", service="recoder-svc", region=REGION)


@pytest.fixture()
def aws_env(monkeypatch):
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
    }.items():
        monkeypatch.setenv(key, value)


def _make_service(desired: int = 2):
    """moto 에 실제 ECS 클러스터·서비스를 만든다."""
    ecs = boto3.client("ecs", region_name=REGION)
    ecs.create_cluster(clusterName=TARGET.cluster)
    ecs.register_task_definition(
        family="recoder-task",
        containerDefinitions=[{"name": "app", "image": "nginx", "memory": 128}],
    )
    ecs.create_service(
        cluster=TARGET.cluster,
        serviceName=TARGET.service,
        taskDefinition="recoder-task",
        desiredCount=desired,
    )
    return ecs


# ---------------------------------------------------------------------------
# 헬스 (ECS · moto)
# ---------------------------------------------------------------------------


@mock_aws
def test_서비스_상태를_읽는다(aws_env):
    _make_service(desired=2)
    health = M.collect_service_health(TARGET)
    assert health.desired == 2
    assert health.running >= 0
    assert health.pending >= 0


@mock_aws
def test_없는_서비스는_감시_실패로_올린다(aws_env):
    """조용히 '정상 0대'로 보고하면 **죽은 서비스를 정상이라고 말하게 된다.**"""
    boto3.client("ecs", region_name=REGION).create_cluster(clusterName=TARGET.cluster)
    with pytest.raises(CloudWatchUnavailableError) as exc:
        M.collect_service_health(TARGET)
    assert TARGET.service in str(exc.value)


@mock_aws
def test_클러스터가_아예_없어도_예외로_올린다(aws_env):
    with pytest.raises(CloudWatchUnavailableError):
        M.collect_service_health(EcsTarget(cluster="없음", service="없음", region=REGION))


def test_중단_태스크_조회가_실패해도_헬스는_돌려준다():
    """중단 태스크는 보조 정보다. 이것 때문에 running/desired 까지 못 보면 안 된다."""
    class _Ecs:
        def describe_services(self, **_kw):
            return {"services": [{"runningCount": 2, "desiredCount": 2, "pendingCount": 0}]}

        def list_tasks(self, **_kw):
            raise RuntimeError("권한 없음")

    health = M._recent_stopped_tasks(_Ecs(), TARGET, 300)
    assert health == (0, "")


def test_관측_창_밖에서_멈춘_태스크는_세지_않는다():
    """어제 한 번 죽은 것까지 세면 오늘 배포가 계속 이상으로 잡힌다."""
    now = datetime.now(timezone.utc)

    class _Ecs:
        def list_tasks(self, **_kw):
            return {"taskArns": ["a", "b"]}

        def describe_tasks(self, **_kw):
            return {"tasks": [
                {"stoppedAt": now - timedelta(seconds=60), "stoppedReason": "최근"},
                {"stoppedAt": now - timedelta(days=1), "stoppedReason": "어제"},
            ]}

    count, reason = M._recent_stopped_tasks(_Ecs(), TARGET, 300)
    assert count == 1, "창 밖 태스크까지 셌다"
    assert reason == "최근"


def test_음성대조_창_안의_태스크는_제대로_센다():
    """전부 걸러내면 위 테스트는 통과해도 크래시 루프를 영영 못 잡는다."""
    now = datetime.now(timezone.utc)

    class _Ecs:
        def list_tasks(self, **_kw):
            return {"taskArns": ["a", "b", "c"]}

        def describe_tasks(self, **_kw):
            return {"tasks": [
                {"stoppedAt": now - timedelta(seconds=10), "stoppedReason": "x"},
                {"stoppedAt": now - timedelta(seconds=20), "stoppedReason": "y"},
                {"stoppedAt": now - timedelta(seconds=30), "stoppedReason": "z"},
            ]}

    count, _ = M._recent_stopped_tasks(_Ecs(), TARGET, 300)
    assert count == 3


# ---------------------------------------------------------------------------
# 트래픽 (CloudWatch · 스텁)
# ---------------------------------------------------------------------------


class _FakeCloudWatch:
    """AWS 문서의 get_metric_statistics 응답 모양을 그대로 흉내낸다."""

    def __init__(self, data: dict, record: list | None = None):
        self._data = data
        self._record = record if record is not None else []

    def get_metric_statistics(self, **kwargs):
        self._record.append(kwargs)
        name = kwargs["MetricName"]
        return self._data.get(name, {"Datapoints": []})


def _with_cw(monkeypatch, fake) -> list:
    calls: list = []
    fake._record = calls
    monkeypatch.setattr(M, "_clients", lambda target, session=None: (object(), fake))
    return calls


ALB_TARGET = EcsTarget(
    cluster="c", service="s", region=REGION,
    load_balancer="app/recoder-alb/abc123", target_group="targetgroup/recoder-tg/def456",
)


def test_ALB_지표를_읽어_창을_채운다(monkeypatch):
    fake = _FakeCloudWatch({
        "RequestCount": {"Datapoints": [{"Sum": 800.0}, {"Sum": 200.0}]},
        "HTTPCode_Target_5XX_Count": {"Datapoints": [{"Sum": 30.0}]},
        "TargetResponseTime": {"Datapoints": [
            {"ExtendedStatistics": {"p95": 1.2}},
            {"ExtendedStatistics": {"p95": 2.4}},
        ]},
    })
    _with_cw(monkeypatch, fake)

    window = M.collect_traffic_metrics(ALB_TARGET, window_seconds=300)

    assert window.requests == 1000, "여러 데이터포인트를 합산하지 않았다"
    assert window.errors_5xx == 30
    assert window.p95_seconds == 2.4, "여러 창의 p95 중 최악을 취해야 한다"
    assert abs(window.error_rate - 0.03) < 1e-9


def test_p95는_ExtendedStatistics_로_요청한다(monkeypatch):
    """Statistics 로는 백분위를 못 얻는다. 잘못 요청하면 항상 None 이 된다."""
    fake = _FakeCloudWatch({})
    calls = _with_cw(monkeypatch, fake)

    M.collect_traffic_metrics(ALB_TARGET, window_seconds=300)

    latency = next(c for c in calls if c["MetricName"] == "TargetResponseTime")
    assert latency.get("ExtendedStatistics") == ["p95"]
    assert "Statistics" not in latency

    counts = next(c for c in calls if c["MetricName"] == "RequestCount")
    assert counts.get("Statistics") == ["Sum"]
    assert counts["Namespace"] == "AWS/ApplicationELB"
    assert counts["Period"] >= 60, "Period 가 60초 미만이면 CloudWatch 가 거부한다"


def test_ALB_가_없으면_지표를_건너뛴다_예외_없이(monkeypatch):
    """내부 서비스·배치는 ALB 가 없다. 그것도 정상이다."""
    called = []
    monkeypatch.setattr(
        M, "_clients", lambda *a, **k: called.append(1) or (object(), _FakeCloudWatch({})),
    )
    window = M.collect_traffic_metrics(TARGET)          # load_balancer 없음
    assert window.requests is None and window.p95_seconds is None
    assert not called, "ALB 가 없는데 CloudWatch 를 불렀다"


def test_지표_조회가_실패하면_0이_아니라_None(monkeypatch):
    """**0 으로 채우면 '에러 0건, 정상'이라고 보고하게 된다.**

    감시가 꺼진 것을 정상으로 말하는 셈이다.
    """
    class _Broken:
        def get_metric_statistics(self, **_kw):
            raise RuntimeError("AccessDenied")

    monkeypatch.setattr(M, "_clients", lambda *a, **k: (object(), _Broken()))
    window = M.collect_traffic_metrics(ALB_TARGET)

    assert window.requests is None
    assert window.errors_5xx is None
    assert window.p95_seconds is None
    assert window.error_rate is None


def test_음성대조_요청이_없던_창은_0으로_읽는다(monkeypatch):
    """CloudWatch 는 0 을 데이터포인트로 남기지 않는다.

    '데이터 없음'을 못 읽음(None)으로 처리하면, 조용한 시간대마다 지표가
    사라져 감시가 사실상 멈춘다. 이건 실제로 0 이다.
    """
    _with_cw(monkeypatch, _FakeCloudWatch({}))
    window = M.collect_traffic_metrics(ALB_TARGET)

    assert window.requests == 0
    assert window.errors_5xx == 0
    #: 다만 응답 시간은 다르다 — 요청이 없으면 0 초가 아니라 '없음'이다.
    assert window.p95_seconds is None


def test_TargetGroup_차원이_함께_전달된다(monkeypatch):
    """LoadBalancer 만 주면 다른 대상 그룹의 트래픽까지 섞여 들어온다."""
    calls = _with_cw(monkeypatch, _FakeCloudWatch({}))
    M.collect_traffic_metrics(ALB_TARGET)

    dims = {d["Name"]: d["Value"] for d in calls[0]["Dimensions"]}
    assert dims["LoadBalancer"] == "app/recoder-alb/abc123"
    assert dims["TargetGroup"] == "targetgroup/recoder-tg/def456"
