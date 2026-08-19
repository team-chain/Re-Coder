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


def test_중단_태스크를_못_읽으면_0이_아니라_None():
    """**예전에는 여기서 0 을 돌려줬고, 테스트가 그걸 고정해 두고 있었다.**

    `ecs:ListTasks` 권한이 없는 인스턴스에서 크래시 루프 감지가 영구히
    죽는데, 스냅샷에는 "중단 0건" 이 남아 정상으로 보였다.
    """
    class _Ecs:
        def list_tasks(self, **_kw):
            raise RuntimeError("권한 없음")

    count, reason = M._recent_stopped_tasks(_Ecs(), TARGET, 300)
    assert count is None, "못 읽은 것을 0건으로 보고하면 감시가 죽은 걸 정상이라 말한다"
    assert reason == ""


def test_못_읽어도_running_desired_는_돌려준다(monkeypatch):
    """중단 태스크는 보조 정보다. 이것 때문에 헬스 전체를 못 보면 안 된다."""
    class _Ecs:
        def describe_services(self, **_kw):
            return {"services": [{"runningCount": 2, "desiredCount": 2, "pendingCount": 0}]}

        def list_tasks(self, **_kw):
            raise RuntimeError("권한 없음")

    monkeypatch.setattr(M, "_clients", lambda *a, **k: (_Ecs(), object()))
    health = M.collect_service_health(TARGET)
    assert health.running == 2 and health.desired == 2
    assert health.stopped_recently is None


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


def test_리전이_비면_무엇을_설정해야_하는지_말한다():
    """botocore 의 `Invalid endpoint: https://ecs..amazonaws.com` 로는

    무엇이 잘못됐는지 알 수 없다. 감시가 안 도는 이유가 로그에 안 보이면
    사람들은 감시가 도는 줄 안다.
    """
    with pytest.raises(CloudWatchUnavailableError) as exc:
        M.collect_service_health(EcsTarget(cluster="c", service="s", region=""))
    assert "RECODER_WATCHDOG_AWS_REGION" in str(exc.value)


def test_음성대조_리전이_있으면_리전_오류를_내지_않는다(monkeypatch):
    """전부 리전 오류로 처리하면 위 테스트는 아무것도 증명하지 못한다."""
    class _Ecs:
        def describe_services(self, **_kw):
            return {"services": [{"runningCount": 1, "desiredCount": 1, "pendingCount": 0}]}

        def list_tasks(self, **_kw):
            return {"taskArns": []}

    monkeypatch.setattr(M, "_clients", lambda *a, **k: (_Ecs(), object()))
    health = M.collect_service_health(TARGET)
    assert health.running == 1


def test_타깃_지표는_TargetGroup_차원을_함께_보낸다(monkeypatch):
    """TargetGroup 을 빼면 다른 대상 그룹의 트래픽까지 섞여 들어온다."""
    calls = _with_cw(monkeypatch, _FakeCloudWatch({}))
    M.collect_traffic_metrics(ALB_TARGET)

    by_name = {c["MetricName"]: {d["Name"]: d["Value"] for d in c["Dimensions"]} for c in calls}
    for metric in ("HTTPCode_Target_5XX_Count", "TargetResponseTime"):
        assert by_name[metric]["TargetGroup"] == "targetgroup/recoder-tg/def456"
        assert by_name[metric]["LoadBalancer"] == "app/recoder-alb/abc123"


def test_ALB_자체_지표는_TargetGroup_차원을_붙이지_않는다(monkeypatch):
    """**이걸 붙이면 최악의 장애가 안 보인다.**

    타깃이 전부 죽으면 ALB 가 자기가 503 을 내는데, 그건 TargetGroup 차원에
    안 잡힌다. RequestCount 도 TargetGroup 으로 읽으면 0 이라, 전면 장애가
    "요청 0건, 에러 0건" 으로 보인다.
    """
    calls = _with_cw(monkeypatch, _FakeCloudWatch({}))
    M.collect_traffic_metrics(ALB_TARGET)

    by_name = {c["MetricName"]: {d["Name"] for d in c["Dimensions"]} for c in calls}
    assert "HTTPCode_ELB_5XX_Count" in by_name, "ALB 자체 5xx 를 아예 안 읽는다"
    assert by_name["HTTPCode_ELB_5XX_Count"] == {"LoadBalancer"}
    assert by_name["RequestCount"] == {"LoadBalancer"}


def test_타깃이_전부_죽은_장애가_지표에_드러난다(monkeypatch):
    """헬시 타깃 0 → ALB 가 503. ECS 는 running==desired 라 조용하다.

    이 경로에서 알림이 안 나가면, 사용자에게 앱이 완전히 죽은 동안
    감시는 "이상 없음" 이라고 말한다.
    """
    fake = _FakeCloudWatch({
        "RequestCount": {"Datapoints": [{"Sum": 300.0}]},
        "HTTPCode_Target_5XX_Count": {"Datapoints": []},        # 타깃까지 안 감
        "HTTPCode_ELB_5XX_Count": {"Datapoints": [{"Sum": 300.0}]},
    })
    _with_cw(monkeypatch, fake)

    window = M.collect_traffic_metrics(ALB_TARGET)
    assert window.requests == 300
    assert window.errors_5xx == 300
    assert window.error_rate == 1.0

    from cloudwatch_thresholds import ServiceHealth, judge
    anomalies = judge(ServiceHealth(running=2, desired=2, stopped_recently=0), window)
    assert any(a.alert_type == "http_5xx_spike" for a in anomalies), "전면 장애인데 조용하다"


def test_음성대조_타깃_5xx만_있어도_합산된다(monkeypatch):
    """ELB 쪽만 보면 반대로 평범한 애플리케이션 오류를 놓친다."""
    fake = _FakeCloudWatch({
        "RequestCount": {"Datapoints": [{"Sum": 1000.0}]},
        "HTTPCode_Target_5XX_Count": {"Datapoints": [{"Sum": 90.0}]},
        "HTTPCode_ELB_5XX_Count": {"Datapoints": []},
    })
    _with_cw(monkeypatch, fake)
    window = M.collect_traffic_metrics(ALB_TARGET)
    assert window.errors_5xx == 90


def test_Period_는_60의_배수로_보낸다(monkeypatch):
    """60의 배수가 아니면 CloudWatch 가 거부하고, 지표가 조용히 사라진다."""
    calls = _with_cw(monkeypatch, _FakeCloudWatch({}))
    M.collect_traffic_metrics(ALB_TARGET, window_seconds=301)
    assert calls, "지표를 아예 안 불렀다"
    for call in calls:
        assert call["Period"] % 60 == 0, f"{call['MetricName']} 의 Period={call['Period']}"
        assert call["Period"] >= 60


# ---------------------------------------------------------------------------
# 크래시 vs 의도적 중단
# ---------------------------------------------------------------------------


def _ecs_with_tasks(tasks: list, pages: int = 1):
    class _Ecs:
        def __init__(self):
            self.page = 0

        def list_tasks(self, **kw):
            self.page += 1
            arns = [f"a{i}" for i in range(len(tasks))]
            if self.page < pages:
                return {"taskArns": arns, "nextToken": f"t{self.page}"}
            return {"taskArns": arns}

        def describe_tasks(self, **kw):
            return {"tasks": tasks}
    return _Ecs()


def test_정상_배포로_멈춘_태스크는_크래시로_세지_않는다():
    """**정상 배포마다 critical 경보가 나가면 아무도 경고를 안 믿는다.**

    desiredCount=2 서비스를 배포하면 옛 태스크 2개가 멈추고, 그것만으로
    task_restarts(기본 2)가 채워져 「크래시 반복」이 나갔다.
    """
    now = datetime.now(timezone.utc)
    tasks = [
        {"stoppedAt": now - timedelta(seconds=30),
         "stoppedReason": "Scaling activity initiated by deployment ecs-svc/123"},
        {"stoppedAt": now - timedelta(seconds=25),
         "stoppedReason": "Scaling activity initiated by deployment ecs-svc/123"},
    ]
    count, _ = M._recent_stopped_tasks(_ecs_with_tasks(tasks), TARGET, 300)
    assert count == 0, "정상 배포를 크래시 루프로 셌다"


def test_음성대조_진짜_크래시는_그대로_센다():
    """전부 걸러내면 위 테스트는 통과해도 크래시 루프를 영영 못 잡는다."""
    now = datetime.now(timezone.utc)
    tasks = [
        {"stoppedAt": now - timedelta(seconds=30),
         "stoppedReason": "Essential container in task exited"},
        {"stoppedAt": now - timedelta(seconds=10),
         "stoppedReason": "OutOfMemoryError: Container killed due to memory usage"},
    ]
    count, reason = M._recent_stopped_tasks(_ecs_with_tasks(tasks), TARGET, 300)
    assert count == 2
    assert "OutOfMemory" in reason, "가장 최근 사유가 아니라 나열 순서를 따랐다"


def test_모르는_사유는_크래시로_센다():
    """새로운 실패 방식을 놓치는 쪽이 더 위험하다 — 화이트리스트가 아니라 블랙리스트."""
    now = datetime.now(timezone.utc)
    tasks = [
        {"stoppedAt": now - timedelta(seconds=5), "stoppedReason": "처음 보는 사유"},
        {"stoppedAt": now - timedelta(seconds=6), "stoppedReason": ""},
    ]
    count, _ = M._recent_stopped_tasks(_ecs_with_tasks(tasks), TARGET, 300)
    assert count == 2


def test_아직_안_멈춘_태스크는_세지_않는다():
    """`desiredStatus=STOPPED` 는 draining 중(stoppedAt 없음)도 돌려준다.

    예전에는 그것들이 창 필터를 그냥 통과해 배포 중 오탐을 키웠다.
    """
    now = datetime.now(timezone.utc)
    tasks = [
        {"stoppedReason": "Essential container in task exited"},          # stoppedAt 없음
        {"stoppedAt": now - timedelta(seconds=10), "stoppedReason": "Essential container in task exited"},
    ]
    count, _ = M._recent_stopped_tasks(_ecs_with_tasks(tasks), TARGET, 300)
    assert count == 1, "아직 안 멈춘 태스크까지 셌다"


def test_중단_태스크_목록을_페이지네이션한다():
    """**장애가 심할수록 조용해지는** 역전을 막는다.

    크래시 루프가 오래되면 STOPPED 태스크가 쌓이는데, 첫 100개만 보면
    그게 전부 창 밖일 수 있다.
    """
    now = datetime.now(timezone.utc)
    tasks = [{"stoppedAt": now, "stoppedReason": "Essential container in task exited"}]
    ecs = _ecs_with_tasks(tasks, pages=3)
    M._recent_stopped_tasks(ecs, TARGET, 300)
    assert ecs.page == 3, f"nextToken 을 안 따라갔다(페이지 {ecs.page})"
