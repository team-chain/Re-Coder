"""
FR-06-01/02 Watchdog — 데몬 배선

수집(cloudwatch_monitor)과 판정(cloudwatch_thresholds)이 각각 맞아도, 데몬이
그걸 부르지 않으면 **도달 불가능한 코드**다. 여기서는 감시 루프가 실제로
지표를 읽고 알림을 내보내는지 본다.

DoD: "일부러 죽인 앱(크래시 코드 배포)에서 '이상' 이벤트가 발생함"
"""
import json
from pathlib import Path

import pytest

import recoder_watchdog as W
from cloudwatch_monitor import CloudWatchUnavailableError
from cloudwatch_thresholds import MetricWindow, ServiceHealth
from config import WatchdogConfig


@pytest.fixture()
def cfg(tmp_path):
    return WatchdogConfig(
        project_id="demo",
        host="test-host",
        environment="test",
        discord_webhook_url=None,          # Discord 없이 파일로만
        incident_path=tmp_path / "incidents.jsonl",
        health_check_urls={},
        poll_interval_seconds=5.0,
        health_interval_seconds=30.0,
        log_level="INFO",
        ecs_cluster="recoder-cluster",
        ecs_service="recoder-svc",
        aws_region="us-east-1",
        unhealthy_polls=3,
    )


def _stub(monkeypatch, health: ServiceHealth, metrics: MetricWindow = MetricWindow()):
    monkeypatch.setattr(W, "collect_service_health", lambda *a, **k: health)
    monkeypatch.setattr(W, "collect_traffic_metrics", lambda *a, **k: metrics)


def _alerts(dog: W.RecoderWatchdog) -> list:
    return list(dog._recent_alerts)


def test_설정이_없으면_ECS_감시를_켜지_않는다(tmp_path):
    """로컬 도커만 쓰는 설치에서도 그대로 돌아야 한다."""
    plain = WatchdogConfig(
        project_id="p", host="h", environment="e", discord_webhook_url=None,
        incident_path=tmp_path / "i.jsonl", health_check_urls={},
        poll_interval_seconds=5.0, health_interval_seconds=30.0, log_level="INFO",
    )
    assert plain.ecs_enabled is False


def test_클러스터와_서비스가_둘_다_있어야_켜진다(cfg):
    assert cfg.ecs_enabled is True
    cfg.ecs_service = ""
    assert cfg.ecs_enabled is False


def test_크래시한_앱에서_이상_이벤트가_발생한다(cfg, monkeypatch):
    """**이 테스트가 이 카드의 DoD 다.**

    태스크가 반복해서 죽는 상황에서 알림이 실제로 나가는지.
    """
    _stub(monkeypatch, ServiceHealth(
        running=0, desired=2, stopped_recently=4,
        last_stopped_reason="Essential container in task exited",
    ))
    dog = W.RecoderWatchdog(cfg)
    dog._ensure_incident_path()

    for _ in range(3):          # 연속 미달 임계치를 채운다
        dog._safe_poll_ecs()

    types = {a["alert_type"] for a in _alerts(dog)}
    assert "ecs_task_restart_loop" in types
    assert "ecs_tasks_unhealthy" in types

    #: 인시던트 파일에도 남아야 한다 — Discord 가 실패해도 기록은 남는 규약.
    lines = cfg.incident_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "인시던트 파일이 비었다"
    record = json.loads(lines[0])
    assert record["source"] == "watchdog"
    assert record["container_name"] == "recoder-cluster/recoder-svc"
    assert record["metric_snapshot"]["desired"] == 2


def test_음성대조_정상이면_아무_알림도_안_나간다(cfg, monkeypatch):
    """항상 알림이 나가면 위 테스트는 아무것도 증명하지 못한다."""
    _stub(monkeypatch, ServiceHealth(running=2, desired=2),
          MetricWindow(requests=500, errors_5xx=1, p95_seconds=0.3))
    dog = W.RecoderWatchdog(cfg)
    dog._ensure_incident_path()

    for _ in range(5):
        dog._safe_poll_ecs()

    assert _alerts(dog) == []
    assert not cfg.incident_path.exists() or not cfg.incident_path.read_text(encoding="utf-8").strip()


def test_정상으로_돌아오면_연속_횟수가_초기화된다(cfg, monkeypatch):
    """초기화 안 하면 며칠에 걸쳐 한 번씩 흔들린 것도 누적돼 결국 경보한다."""
    dog = W.RecoderWatchdog(cfg)
    dog._ensure_incident_path()

    _stub(monkeypatch, ServiceHealth(running=1, desired=2))
    dog._safe_poll_ecs()
    dog._safe_poll_ecs()
    assert dog._ecs_unhealthy_streak == 2

    _stub(monkeypatch, ServiceHealth(running=2, desired=2))
    dog._safe_poll_ecs()
    assert dog._ecs_unhealthy_streak == 0


def test_AWS를_못_읽어도_데몬이_죽지_않는다(cfg, monkeypatch):
    def _boom(*_a, **_k):
        raise CloudWatchUnavailableError("AccessDenied")

    monkeypatch.setattr(W, "collect_service_health", _boom)
    dog = W.RecoderWatchdog(cfg)
    dog._ensure_incident_path()

    dog._safe_poll_ecs()        # 예외가 밖으로 나오면 안 된다
    assert _alerts(dog) == [], "일시적 실패에 바로 알림을 내면 시끄럽다"


def test_오래_못_읽으면_감시가_꺼진_사실을_알린다(cfg, monkeypatch):
    """**조용히 넘어가면 앱이 죽어도 아무 알림이 안 온다.**

    감시 실패를 정상으로 두는 것이 이 기능에서 가장 위험한 실패 방식이다.
    """
    #: **데몬이 실제로 잡는 클래스**로 던진다. recoder_watchdog 은 실행 경로에
    #: 따라 watchdog.cloudwatch_monitor 를 import 하므로, 평평한 경로로 import 한
    #: 클래스와 객체가 다를 수 있다. 계약을 검사하려면 데몬 쪽 것을 써야 한다.
    monkeypatch.setattr(
        W, "collect_service_health",
        lambda *a, **k: (_ for _ in ()).throw(W.CloudWatchUnavailableError("AccessDenied")),
    )
    dog = W.RecoderWatchdog(cfg)
    dog._ensure_incident_path()

    dog._safe_poll_ecs()
    #: 3분 전부터 실패해 온 것으로 만든다.
    dog._ecs_unavailable_since -= 200
    dog._safe_poll_ecs()

    types = {a["alert_type"] for a in _alerts(dog)}
    assert "ecs_monitoring_unavailable" in types
    message = next(a for a in _alerts(dog) if a["alert_type"] == "ecs_monitoring_unavailable")["message"]
    assert "감시되지 않고" in message, "무엇이 문제인지 안 알려준다"


def test_예기치_못한_오류도_데몬을_죽이지_않는다(cfg, monkeypatch):
    monkeypatch.setattr(
        W, "collect_service_health",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("이상한 응답")),
    )
    dog = W.RecoderWatchdog(cfg)
    dog._ensure_incident_path()
    dog._safe_poll_ecs()        # 예외가 밖으로 나오면 안 된다
    assert _alerts(dog) == [], "한 번 실패에 바로 알림을 내면 시끄럽다"


def test_전용_분기를_비껴간_오류도_결국_드러난다(cfg, monkeypatch):
    """**감시 실패를 조용히 넘기는 것이 이 기능의 최악 실패다.**

    실제로 그런 경로가 있었다 — import 경로에 따라
    CloudWatchUnavailableError 의 클래스 객체가 달라지면 전용 except 를
    비껴가 generic 핸들러에 걸리고, 예전 구현은 거기서 로그만 남겼다.
    그 동안 앱이 죽어도 사용자에게는 아무 알림이 없다.
    """
    monkeypatch.setattr(
        W, "collect_service_health",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("전용 분기를 안 타는 오류")),
    )
    dog = W.RecoderWatchdog(cfg)
    dog._ensure_incident_path()

    dog._safe_poll_ecs()
    dog._ecs_unavailable_since -= 200
    dog._safe_poll_ecs()

    types = {a["alert_type"] for a in _alerts(dog)}
    assert "ecs_monitoring_unavailable" in types, "감시가 죽었는데 아무도 모른다"


# ---------------------------------------------------------------------------
# 설치·설정 — **코드가 맞아도 켜지지 않으면 없는 기능이다.**
# ---------------------------------------------------------------------------

WATCHDOG_DIR = Path(__file__).resolve().parents[1]


def test_설치_템플릿에_ECS_설정_항목이_있다():
    """**이 테스트가 없어서 실제로 놓칠 뻔했다.**

    install.sh 가 /etc/recoder/watchdog.env 를 고정 템플릿으로 만든다.
    거기에 ECS 항목이 없으면 설치된 데몬은 ecs_enabled=False 로만 돌고,
    이 브랜치의 코드는 **한 줄도 실행되지 않는다.** 테스트는 전부 통과하고,
    앱이 죽어도 알림은 없다.
    """
    script = (WATCHDOG_DIR / "install.sh").read_text(encoding="utf-8")
    for key in (
        "RECODER_WATCHDOG_ECS_CLUSTER",
        "RECODER_WATCHDOG_ECS_SERVICE",
        "RECODER_WATCHDOG_AWS_REGION",
        "RECODER_WATCHDOG_ALB_NAME",
        "RECODER_WATCHDOG_UNHEALTHY_POLLS",
    ):
        assert key in script, f"설치 템플릿에 {key} 가 없다 — 감시가 영영 안 켜진다"


def test_설치_스크립트가_watchdog_의_모든_모듈을_복사한다():
    """**이 검사가 없어서 데몬 전체가 죽는 결함을 놓쳤다.**

    install.sh 가 복사할 파일을 손으로 나열하고 있었고, 새 모듈 두 개를
    거기 넣는 걸 잊었다. recoder_watchdog.py 는 그걸 최상단에서 import
    하므로 설치된 데몬이 ModuleNotFoundError 로 죽었다 — ECS 감시만이
    아니라 컨테이너 크래시·OOM 감시까지 통째로.

    앞의 환경변수 검사는 통과했다. 키만 grep 했지 파일 목록은 안 봤기
    때문이다. 그래서 여기서는 **실제 파일 목록**과 대조한다.
    """
    script = (WATCHDOG_DIR / "install.sh").read_text(encoding="utf-8")
    modules = sorted(p.name for p in WATCHDOG_DIR.glob("*.py"))
    assert modules, "watchdog 에 .py 가 하나도 없다"

    #: 목록을 손으로 유지하는 방식 자체가 결함이었으므로, 개별 이름이 아니라
    #: "디렉토리의 .py 를 전부 가져오는가" 를 본다.
    assert '"${SCRIPT_DIR}"/*.py' in script, (
        "install.sh 가 아직 파일을 손으로 나열한다 — 새 모듈을 또 빠뜨린다. "
        f"현재 모듈 {len(modules)}개: {modules}"
    )


def test_설치_스크립트가_boto3_를_설치한다():
    """없으면 모든 ECS 폴링이 실패하고, 메시지는 엉뚱하게 자격증명을 가리킨다."""
    script = (WATCHDOG_DIR / "install.sh").read_text(encoding="utf-8")
    assert "boto3" in script


def test_감시_모듈이_없어도_데몬은_계속_돈다():
    """**감시 도구가 자기 확장 기능 때문에 죽는 것이 가장 나쁜 실패다.**

    ECS 모듈을 못 불러와도 컨테이너 크래시·OOM 감시는 계속돼야 한다.
    """
    assert hasattr(W, "ecs_monitoring_importable")
    assert W.ecs_monitoring_importable() is None, "정상 환경인데 import 가 깨졌다"

    source = (WATCHDOG_DIR / "recoder_watchdog.py").read_text(encoding="utf-8")
    head = source[: source.index("log = logging.getLogger")]
    assert "cloudwatch_monitor" not in head, (
        "ECS 모듈이 필수 import 블록에 있다 — 없으면 데몬 전체가 죽는다"
    )


def test_ECS_모듈이_없으면_폴링을_시도하지_않는다(cfg, monkeypatch):
    """설정만 보고 돌면 매 주기 NameError 가 난다."""
    monkeypatch.setattr(W, "_ECS_IMPORT_ERROR", "No module named 'cloudwatch_monitor'")
    dog = W.RecoderWatchdog(cfg)
    assert cfg.ecs_enabled is True
    assert dog._ecs_active is False


def test_음성대조_모듈이_있으면_폴링이_켜진다(cfg):
    assert W.RecoderWatchdog(cfg)._ecs_active is True


def test_재시작_임계치도_환경변수로_조절된다(cfg):
    """이것만 환경변수가 없어서, 오탐이 나도 운영자가 손쓸 수 없었다."""
    assert hasattr(cfg, "task_restarts")
    script = (WATCHDOG_DIR / "install.sh").read_text(encoding="utf-8")
    assert "RECODER_WATCHDOG_TASK_RESTARTS" in script


def test_음성대조_템플릿_검사가_아무_문자열이나_통과시키지_않는다():
    """위 테스트가 항상 참이면 아무것도 증명하지 못한다."""
    script = (WATCHDOG_DIR / "install.sh").read_text(encoding="utf-8")
    assert "RECODER_WATCHDOG_ECS_없는키" not in script


def test_리전을_빼먹으면_설정_오류로_알려준다(cfg):
    """botocore 는 `Invalid endpoint: https://ecs..amazonaws.com` 만 낸다.

    그 메시지로는 무엇을 고쳐야 하는지 알 수 없어서, 감시가 안 도는데도
    도는 줄 알게 된다.
    """
    cfg.aws_region = ""
    problem = cfg.ecs_config_problem()
    assert problem is not None
    assert "RECODER_WATCHDOG_AWS_REGION" in problem


def test_음성대조_제대로_설정하면_문제_없음(cfg):
    assert cfg.ecs_config_problem() is None


def test_ECS를_안_쓰는_설치는_설정_오류가_아니다(tmp_path):
    """로컬 도커만 쓰는 설치가 --check 에서 실패하면 안 된다."""
    from config import WatchdogConfig as C
    plain = C(
        project_id="p", host="h", environment="e", discord_webhook_url=None,
        incident_path=tmp_path / "i.jsonl", health_check_urls={},
        poll_interval_seconds=5.0, health_interval_seconds=30.0, log_level="INFO",
    )
    assert plain.ecs_config_problem() is None
    assert "ecs=off" in plain.summary(), "감시가 꺼진 사실이 로그에 안 보인다"


def test_요약에_감시_대상이_드러난다(cfg):
    """--check 로도 감시가 켜졌는지 알 수 없으면, 꺼진 채 돌아도 아무도 모른다."""
    summary = cfg.summary()
    assert "recoder-cluster/recoder-svc" in summary
    assert "us-east-1" in summary


def test_지표가_알림에_함께_실린다(cfg, monkeypatch):
    """알림만 오고 수치가 없으면 사용자는 롤백 여부를 판단할 수 없다."""
    _stub(monkeypatch,
          ServiceHealth(running=2, desired=2),
          MetricWindow(requests=400, errors_5xx=80, p95_seconds=4.5))
    dog = W.RecoderWatchdog(cfg)
    dog._ensure_incident_path()
    dog._safe_poll_ecs()

    spike = next(a for a in _alerts(dog) if a["alert_type"] == "http_5xx_spike")
    snap = spike["metric_snapshot"]
    assert snap["requests"] == 400
    assert snap["errors_5xx"] == 80
    assert abs(snap["error_rate"] - 0.2) < 1e-9
    assert snap["p95_seconds"] == 4.5
