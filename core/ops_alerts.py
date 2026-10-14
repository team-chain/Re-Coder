"""공격·장애 감지 알림 (2.0.25).

무엇을 보나 (방패 nginx 의 JSON 접속 기록 → CloudWatch Logs 지표 필터)
    Requests  방패를 지난 모든 요청            Blocked  방패가 429 로 막은 요청          Errors  앱이 낸 5xx
    + ECS 서비스의 실행 태스크 수 < 원하는 수(장애)

판정 (evaluate — 순수 함수, 고정 값으로 단위 테스트)
    요청 급증   최근 5분 요청 수가 지난 24시간(5분 단위) 중앙값의 10배 이상 (기록이 하루 미만이면 5분 3,000회 고정 기준)
    차단 급증   최근 5분 429 비율 20% 초과(요청 50건 이상일 때만)
    오류 증가   최근 5분 5xx 비율 5% 초과(요청 50건 이상일 때만)
    장애        실행 태스크 < 원하는 수 가 3번 연속(1분 간격)

알림 두 경로
    · Core 가 켜져 있을 때: 1분마다 지표를 읽어 판정 → /api/ops/alerts → 확장이 VS Code 알림·디스코드로.
    · PC 가 꺼져 있을 때: 배포할 때 CloudWatch 경보 + SNS 주제(이메일 구독)를 만들어 AWS 가 직접 메일을 보낸다.

대응 제안 (적용은 승인 뒤에만 — act)
    원인 IP 차단(방패 설정에 deny 추가 → 새 태스크 정의 리비전) · 방패 엄격 · 태스크 수 늘리기 · (롤백은 기존 롤백 제안 흐름)

권한이 없으면(옛 권한표) 이 기능만 빠지고 배포는 그대로 된다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PERIOD_S = 300
FIRST_DAY_THRESHOLD = 3000          # 기록이 하루 미만일 때 5분 요청 수 고정 기준
SPIKE_FACTOR = 10.0
BLOCK_RATIO = 0.20
ERROR_RATIO = 0.05
MIN_REQUESTS_FOR_RATIO = 50
TASK_SHORT_STRIKES = 3
COOLDOWN_S = 30 * 60
METRICS = ("Requests", "Blocked", "Errors")
_FILTERS = {
    "Requests": '{ $.src = "recoder-shield" }',
    "Blocked": '{ ($.src = "recoder-shield") && ($.status = 429) }',
    "Errors": '{ ($.src = "recoder-shield") && ($.status >= 500) }',
}
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def namespace(service: str) -> str:
    return f"ReCoder/{_SAFE.sub('-', service)[:200]}"


def _name(service: str, what: str) -> str:
    return f"recoder-{_SAFE.sub('-', service)[:120]}-{what}"


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------

def evaluate(series: dict[str, list[float]], *, history_points: Optional[int] = None) -> list[dict]:
    """5분 단위 합계 시계열(오래된 것 → 최근) → 알림 [{kind, severity, title, detail, value, baseline}].

    series 에는 Requests·Blocked·Errors 가 같은 길이로 들어온다(없는 칸은 0)."""
    req = [float(x or 0) for x in series.get("Requests") or []]
    if not req:
        return []
    blocked = [float(x or 0) for x in (series.get("Blocked") or [0] * len(req))]
    errors = [float(x or 0) for x in (series.get("Errors") or [0] * len(req))]
    now, past = req[-1], req[:-1]
    out: list[dict] = []
    points = history_points if history_points is not None else len(past)
    if points >= 288 - 1:
        median = statistics.median(past[-288:]) if past else 0.0
        threshold = max(median * SPIKE_FACTOR, 300.0)  # 아주 조용한 서비스의 작은 흔들림은 무시
        basis = f"지난 24시간 5분 중앙값 {median:,.0f}회의 {SPIKE_FACTOR:.0f}배"
    else:
        median = statistics.median(past) if past else 0.0
        threshold = float(FIRST_DAY_THRESHOLD)
        basis = f"첫날 고정 기준 5분 {FIRST_DAY_THRESHOLD:,}회"
    if now >= threshold:
        out.append({"kind": "spike", "severity": "high", "title": "요청 급증",
                    "detail": f"최근 5분 요청 {now:,.0f}회 — {basis} 이상", "value": now, "baseline": median})
    if now >= MIN_REQUESTS_FOR_RATIO:
        b = blocked[-1] / now if now else 0.0
        if b > BLOCK_RATIO:
            out.append({"kind": "blocked", "severity": "high", "title": "차단 급증",
                        "detail": f"최근 5분 요청의 {b:.0%}를 방패가 막았습니다(기준 {BLOCK_RATIO:.0%})", "value": b, "baseline": BLOCK_RATIO})
        e = errors[-1] / now if now else 0.0
        if e > ERROR_RATIO:
            out.append({"kind": "errors", "severity": "high", "title": "오류 증가",
                        "detail": f"최근 5분 요청의 {e:.1%}가 서버 오류(5xx)입니다(기준 {ERROR_RATIO:.0%})", "value": e, "baseline": ERROR_RATIO})
    return out


class TaskWatch:
    """실행 태스크 < 원하는 수 가 연속 N번이면 장애."""

    def __init__(self, strikes: int = TASK_SHORT_STRIKES) -> None:
        self.strikes = strikes
        self.count: dict[str, int] = {}

    def observe(self, key: str, running: int, desired: int) -> Optional[dict]:
        if desired > 0 and running < desired:
            self.count[key] = self.count.get(key, 0) + 1
        else:
            self.count[key] = 0
        if self.count[key] == self.strikes:
            return {"kind": "outage", "severity": "critical", "title": "장애 — 실행 태스크 부족",
                    "detail": f"실행 중 {running} / 원하는 수 {desired} 가 {self.strikes}번 연속(약 {self.strikes}분)입니다",
                    "value": running, "baseline": desired}
        return None


# ---------------------------------------------------------------------------
# 배포 때 AWS 쪽 준비 (지표 필터 · 경보 · SNS)
# ---------------------------------------------------------------------------

def setup_aws(clients: dict, *, service: str, log_group: str, email: str = "", tags: Optional[dict] = None) -> dict:
    """best-effort. 결과 {"filters": [...], "alarms": [...], "topic": arn|"", "email": "pending"|"", "notes": [...]}."""
    out: dict[str, Any] = {"filters": [], "alarms": [], "topic": "", "email": "", "notes": []}
    ns = namespace(service)
    logs, cw, sns = clients.get("logs"), clients.get("cloudwatch"), clients.get("sns")
    tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]
    for metric, pattern in _FILTERS.items():
        try:
            logs.put_metric_filter(logGroupName=log_group, filterName=_name(service, metric.lower()), filterPattern=pattern,
                                   metricTransformations=[{"metricName": metric, "metricNamespace": ns, "metricValue": "1",
                                                           "defaultValue": 0.0, "unit": "Count"}])
            out["filters"].append(metric)
        except Exception as exc:  # noqa: BLE001
            out["notes"].append(f"요청 지표({metric})를 만들지 못했습니다: {_err(exc)} — 권한표에 logs:PutMetricFilter 를 추가하세요.")
            break
    topic = ""
    if sns is not None:
        try:
            kwargs = {"Name": _name(service, "alerts")}
            if tag_list:
                kwargs["Tags"] = tag_list
            topic = str(sns.create_topic(**kwargs)["TopicArn"])
            out["topic"] = topic
            if email:
                sns.subscribe(TopicArn=topic, Protocol="email", Endpoint=email)
                out["email"] = "pending"
                out["notes"].append(f"{email} 로 구독 확인 메일을 보냈습니다 — 메일의 확인 링크를 눌러야 PC 가 꺼져 있어도 알림을 받습니다.")
        except Exception as exc:  # noqa: BLE001
            out["notes"].append(f"알림 주제(SNS)를 만들지 못했습니다: {_err(exc)} — 메일 알림 없이 Core 알림만 씁니다.")
    if out["filters"] and cw is not None:
        actions = {"AlarmActions": [topic], "OKActions": []} if topic else {}
        alarms = [
            (_name(service, "traffic-spike"), "요청 급증(5분 요청 수가 첫날 기준을 넘음)", {
                "Namespace": ns, "MetricName": "Requests", "Statistic": "Sum", "Period": PERIOD_S,
                "EvaluationPeriods": 1, "Threshold": float(FIRST_DAY_THRESHOLD), "ComparisonOperator": "GreaterThanOrEqualToThreshold"}),
            (_name(service, "blocked-ratio"), "차단 급증(429 비율 20% 초과)", _ratio_alarm(ns, "Blocked", BLOCK_RATIO)),
            (_name(service, "error-ratio"), "오류 증가(5xx 비율 5% 초과)", _ratio_alarm(ns, "Errors", ERROR_RATIO)),
        ]
        for name, desc, spec in alarms:
            try:
                cw.put_metric_alarm(AlarmName=name, AlarmDescription=f"ReCoder — {desc}", TreatMissingData="notBreaching",
                                    ActionsEnabled=bool(topic), **actions, **spec, **({"Tags": tag_list} if tag_list else {}))
                out["alarms"].append(name)
            except Exception as exc:  # noqa: BLE001
                out["notes"].append(f"CloudWatch 경보를 만들지 못했습니다: {_err(exc)} — 권한표에 cloudwatch:PutMetricAlarm 을 추가하세요.")
                break
    return out


def _ratio_alarm(ns: str, metric: str, ratio: float) -> dict:
    def q(mid: str, name: str) -> dict:
        return {"Id": mid, "ReturnData": False, "MetricStat": {"Metric": {"Namespace": ns, "MetricName": name},
                                                                "Period": PERIOD_S, "Stat": "Sum"}}
    return {"EvaluationPeriods": 1, "Threshold": float(ratio), "ComparisonOperator": "GreaterThanThreshold",
            "Metrics": [q("req", "Requests"), q("bad", metric),
                        {"Id": "ratio", "ReturnData": True, "Label": f"{metric} ratio",
                         "Expression": f"IF(req >= {MIN_REQUESTS_FOR_RATIO}, bad / req, 0)"}]}


def _err(exc: Exception) -> str:
    try:
        return str(exc.response["Error"]["Code"])  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return type(exc).__name__


# ---------------------------------------------------------------------------
# 감시 목록 · 알림 저장
# ---------------------------------------------------------------------------

def _home() -> Path:
    return Path(os.environ.get("RECODER_HOME") or Path.home() / ".recoder")


_LOCK = threading.Lock()


def _read(name: str, default):
    try:
        return json.loads((_home() / name).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _write(name: str, data) -> None:
    path = _home() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def register(*, region: str, cluster: str, service: str, log_group: str, health_path: str = "/health",
             metrics: bool = True) -> dict:
    key = f"{region}/{cluster}/{service}"
    item = {"key": key, "region": region, "cluster": cluster, "service": service, "log_group": log_group,
            "namespace": namespace(service), "health_path": health_path, "metrics": metrics,
            "since": datetime.now(timezone.utc).isoformat()}
    with _LOCK:
        monitors = [m for m in _read("ops_monitors.json", []) if m.get("key") != key]
        monitors.append(item)
        _write("ops_monitors.json", monitors)
    return item


def unregister(key: str) -> bool:
    with _LOCK:
        monitors = _read("ops_monitors.json", [])
        left = [m for m in monitors if m.get("key") != key]
        _write("ops_monitors.json", left)
    return len(left) != len(monitors)


def monitors() -> list[dict]:
    return list(_read("ops_monitors.json", []))


def alerts(since: str = "") -> list[dict]:
    items = _read("ops_alerts.json", [])
    if since:
        ids = [a.get("id") for a in items]
        if since in ids:
            items = items[ids.index(since) + 1:]
    return items


def _store(alert: dict) -> Optional[dict]:
    """같은 서비스·종류는 30분에 한 번만."""
    with _LOCK:
        items = _read("ops_alerts.json", [])
        now = time.time()
        for a in reversed(items):
            if a.get("key") == alert["key"] and a.get("kind") == alert["kind"] and now - float(a.get("ts", 0)) < COOLDOWN_S:
                return None
        alert = {"id": uuid.uuid4().hex[:12], "ts": now, "at": datetime.now(timezone.utc).isoformat(), **alert}
        items = (items + [alert])[-200:]
        _write("ops_alerts.json", items)
    return alert


def _resolve(alert_id: str, status: str, note: str) -> None:
    with _LOCK:
        items = _read("ops_alerts.json", [])
        for a in items:
            if a.get("id") == alert_id:
                a["status"] = status
                a["resolution"] = note
        _write("ops_alerts.json", items)


# ---------------------------------------------------------------------------
# 1분 감시
# ---------------------------------------------------------------------------

def read_series(cw, ns: str, *, hours: int = 24, now: Optional[datetime] = None) -> tuple[dict[str, list[float]], int]:
    """5분 합계 시계열(오래된 → 최근)과 실제로 값이 있던 칸 수."""
    end = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % 5)
    start = end - timedelta(hours=hours)
    queries = [{"Id": m.lower(), "ReturnData": True,
                "MetricStat": {"Metric": {"Namespace": ns, "MetricName": m}, "Period": PERIOD_S, "Stat": "Sum"}} for m in METRICS]
    resp = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end, ScanBy="TimestampAscending")
    slots = int(hours * 3600 / PERIOD_S)
    out: dict[str, list[float]] = {}
    seen = 0
    for r in resp.get("MetricDataResults", []):
        by_time = {t.replace(tzinfo=t.tzinfo or timezone.utc): v for t, v in zip(r.get("Timestamps", []), r.get("Values", []))}
        series = []
        for i in range(slots):
            t = start + timedelta(seconds=i * PERIOD_S)
            series.append(float(by_time.get(t, 0.0)))
        name = next((m for m in METRICS if m.lower() == r.get("Id")), r.get("Id"))
        out[name] = series
        if name == "Requests":
            seen = sum(1 for t in by_time)
    return out, seen


def top_sources(logs, log_group: str, minutes: int = 10, wait_s: float = 12.0, sleep=time.sleep) -> dict:
    """Logs Insights 로 상위 IP·경로 5개. 실패하면 빈 값."""
    now = int(time.time())
    out = {"ips": [], "paths": []}
    for kind, field in (("ips", "remote_addr"), ("paths", "uri")):
        try:
            qid = logs.start_query(logGroupName=log_group, startTime=now - minutes * 60, endTime=now, limit=5,
                                   queryString=(f'filter src = "recoder-shield" | stats count(*) as n by {field} '
                                                "| sort n desc | limit 5"))["queryId"]
            deadline = time.monotonic() + wait_s
            while True:
                res = logs.get_query_results(queryId=qid)
                if res.get("status") in ("Complete", "Failed", "Cancelled", "Timeout") or time.monotonic() > deadline:
                    break
                sleep(1.0)
            total = 0
            rows = []
            for row in res.get("results", []) or []:
                cells = {c.get("field"): c.get("value") for c in row}
                n = int(float(cells.get("n") or 0))
                rows.append({"value": cells.get(field, ""), "count": n})
                total += n
            out[kind] = rows
        except Exception as exc:  # noqa: BLE001
            logger.info("Logs Insights 조회 생략(%s): %s", kind, _err(exc))
    return out


def suggestions(alert: dict, sources: dict, shield_on: bool) -> list[dict]:
    """대응 제안 — 적용은 승인 뒤(act)."""
    out = []
    ips = sources.get("ips") or []
    try:
        from shield import deniable  # type: ignore
    except ImportError:  # pragma: no cover
        from core.shield import deniable  # type: ignore
    if alert["kind"] in ("spike", "blocked") and ips and shield_on and deniable(ips[0]["value"]):
        out.append({"action": "block_ip", "value": ips[0]["value"], "label": f"{ips[0]['value']} 차단",
                    "detail": "방패 설정에 이 IP 를 막는 줄을 넣어 새 버전으로 다시 띄웁니다(무중단 교체)."})
    if alert["kind"] in ("spike", "blocked") and shield_on:
        out.append({"action": "shield_strict", "value": "strict", "label": "방패 강도 엄격으로",
                    "detail": "IP당 초당 5회로 낮춥니다. 정상 사용자도 느려질 수 있습니다."})
    if alert["kind"] in ("spike", "errors", "outage"):
        out.append({"action": "scale", "value": 2, "label": "태스크 2개로", "detail": "요청을 나눠 받습니다(실행 요금이 2배)."})
    if alert["kind"] in ("errors", "outage"):
        out.append({"action": "rollback", "value": "", "label": "이전 버전으로 롤백",
                    "detail": "배포 센터의 롤백 제안으로 이전 태스크 정의로 되돌립니다."})
    return out


class Monitor:
    """Core 안에서 1분마다 감시. session_factory(region) → boto3 Session."""

    def __init__(self, session_factory: Callable[[str], Any], *, interval: float = 60.0,
                 notify: Optional[Callable[[dict], None]] = None) -> None:
        self.session_factory = session_factory
        self.interval = interval
        self.notify = notify
        self.tasks = TaskWatch()

    def tick(self, now: Optional[datetime] = None) -> list[dict]:
        raised = []
        for m in monitors():
            try:
                session = self.session_factory(m["region"])
                ecs = session.client("ecs")
                svc = (ecs.describe_services(cluster=m["cluster"], services=[m["service"]]).get("services") or [{}])[0]
                if svc.get("status") not in (None, "ACTIVE"):
                    continue
                desired, running = int(svc.get("desiredCount", 0) or 0), int(svc.get("runningCount", 0) or 0)
                found = []
                outage = self.tasks.observe(m["key"], running, desired)
                if outage:
                    found.append(outage)
                if m.get("metrics", True):
                    series, seen = read_series(session.client("cloudwatch"), m["namespace"], now=now)
                    found += evaluate(series, history_points=seen)
                for alert in found:
                    alert = {**alert, "key": m["key"], "region": m["region"], "cluster": m["cluster"], "service": m["service"],
                             "status": "open"}
                    sources = top_sources(session.client("logs"), m["log_group"]) if alert["kind"] in ("spike", "blocked", "errors") else {}
                    alert["sources"] = sources
                    alert["suggestions"] = suggestions(alert, sources, m.get("metrics", True))
                    if alert["kind"] == "outage":
                        alert["diagnosis"] = self._diagnose(session, m)
                    stored = _store(alert)
                    if stored:
                        raised.append(stored)
                        if self.notify:
                            try:
                                self.notify(stored)
                            except Exception:  # noqa: BLE001
                                pass
            except Exception as exc:  # noqa: BLE001 - 한 서비스 실패가 다른 감시를 막지 않는다
                logger.info("감시 생략 %s: %s", m.get("key"), _err(exc))
        return raised

    @staticmethod
    def _diagnose(session, m: dict) -> dict:
        try:
            try:
                import aws_infra  # type: ignore
            except ImportError:  # pragma: no cover
                from core import aws_infra  # type: ignore
            return aws_infra.diagnose_stopped_tasks(session.client("ecs"), session.client("logs"), cluster=m["cluster"],
                                                    service=m["service"], log_group=m["log_group"], app_container="app")
        except Exception as exc:  # noqa: BLE001
            return {"error": _err(exc)}

    async def run_forever(self) -> None:
        import asyncio
        while True:
            try:
                if monitors():
                    await asyncio.to_thread(self.tick)
            except Exception:  # noqa: BLE001
                logger.warning("운영 감시 한 바퀴 실패", exc_info=True)
            await asyncio.sleep(self.interval)


# ---------------------------------------------------------------------------
# 대응 적용 (승인 뒤)
# ---------------------------------------------------------------------------

def act(alert_id: str, action: str, value: Any, *, approved: bool, session_factory: Callable[[str], Any]) -> dict:
    if not approved:
        _resolve(alert_id, "dismissed", "사용자가 적용하지 않음")
        return {"applied": False, "message": "적용하지 않았습니다."}
    alert = next((a for a in alerts() if a.get("id") == alert_id), None)
    if alert is None:
        raise ValueError("알림을 찾지 못했습니다.")
    allowed = {s["action"] for s in alert.get("suggestions") or []}
    if action not in allowed:
        raise ValueError("이 알림에 대한 제안이 아닙니다.")
    session = session_factory(alert["region"])
    ecs = session.client("ecs")
    try:
        from shield import reconfigure  # type: ignore
    except ImportError:  # pragma: no cover
        from core.shield import reconfigure  # type: ignore
    if action == "scale":
        count = max(1, min(10, int(value or 2)))
        ecs.update_service(cluster=alert["cluster"], service=alert["service"], desiredCount=count)
        message = f"태스크 수를 {count}개로 바꿨습니다."
    elif action in ("block_ip", "shield_strict"):
        svc = ecs.describe_services(cluster=alert["cluster"], services=[alert["service"]])["services"][0]
        td = ecs.describe_task_definition(taskDefinition=svc["taskDefinition"], include=["TAGS"])
        current = td["taskDefinition"]
        health = next((m.get("health_path") for m in monitors() if m.get("key") == alert["key"]), "/health") or "/health"
        new = reconfigure(current, level="strict" if action == "shield_strict" else None,
                          deny_add=[str(value)] if action == "block_ip" else None, health_path=health)
        register_args = {k: new[k] for k in ("family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions",
                                             "volumes", "placementConstraints", "requiresCompatibilities", "cpu", "memory",
                                             "runtimePlatform") if new.get(k) not in (None, [], "")}
        arn = ecs.register_task_definition(**register_args)["taskDefinition"]["taskDefinitionArn"]
        if td.get("tags"):
            try:
                import aws_infra  # type: ignore
            except ImportError:  # pragma: no cover
                from core import aws_infra  # type: ignore
            aws_infra.tag_created(ecs, "ecs", arn, {t["key"]: t["value"] for t in td["tags"] if "key" in t})
        ecs.update_service(cluster=alert["cluster"], service=alert["service"], taskDefinition=arn, forceNewDeployment=True)
        message = ("방패에 차단 IP 를 넣은" if action == "block_ip" else "방패를 엄격으로 바꾼") + f" 새 버전으로 교체를 시작했습니다({arn.rsplit('/', 1)[-1]})."
    elif action == "rollback":
        message = "배포 센터의 롤백 제안에서 이전 버전을 승인하세요."
    else:
        raise ValueError("모르는 대응입니다.")
    _resolve(alert_id, "applied", message)
    return {"applied": action != "rollback", "message": message}
