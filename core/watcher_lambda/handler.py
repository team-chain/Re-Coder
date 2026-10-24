"""ReCoder 24시간 감시자 (2.0.48) — 사용자 AWS 계정 안에서 도는 Lambda.

이 파일 하나가 그대로 Lambda 코드가 된다(외부 패키지 없음 — Lambda 런타임의 boto3 만 쓴다).
감시자 역할(recoder-watcher) 권한으로 돈다. 그 권한표는 core/aws_policy.watcher_role_policy() 이고,
tests/test_watcher.py 가 이 파일의 AWS 호출과 대조한다.

언제 깨어나나
    · CloudWatch 경보가 ALARM 이 될 때(EventBridge "CloudWatch Alarm State Change") — 요청 급증·차단 급증·오류 증가
    · ECS 가 서비스 이상을 알릴 때(EventBridge "ECS Service Action" · "ECS Deployment State Change")
    · 컨테이너가 멈출 때(EventBridge "ECS Task State Change", STOPPED)
    · 5분마다(EventBridge 일정) — 실행 태스크 < 원하는 수 가 3번 연속이면 장애, 그리고 살아 있다는 기록(heartbeat)
    · 사람이 승인 링크를 열 때(Lambda 함수 URL) — GET 은 확인 화면만, POST 만 실행

원칙
    · 밤에는 감지 · 기록 · 알림 · 준비까지만 한다. 실행은 사람이 승인한 롤백 하나뿐이다.
    · 승인 링크는 일회용(256비트 무작위 토큰, 해시만 저장) · 12시간 만료 · 그 사이 버전이 바뀌었으면 실행하지 않는다.
    · 기록은 이 계정의 DynamoDB 표에만 남는다(30일 뒤 자동 삭제). ReCoder 서버로 보내지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

VERSION = "2.0.48"
EVENT_TTL_S = 30 * 24 * 3600
TOKEN_TTL_S = 12 * 3600
COOLDOWN_S = 30 * 60
#: 종류별 쉼 — 서비스가 사라진 것은 하루에 한 번만
COOLDOWNS = {"missing": 24 * 3600}
OUTAGE_STRIKES = 3
#: 경보 이름 끝 → 종류 (core/ops_alerts.setup_aws 가 만드는 이름과 같다)
ALARM_KINDS = {"traffic-spike": "spike", "blocked-ratio": "blocked", "error-ratio": "errors"}
#: 이 종류가 생기면 이전 버전 롤백 링크를 함께 준비한다(요청 급증·차단은 버전 문제가 아니라 공격이므로 제외).
ROLLBACK_KINDS = {"errors", "outage", "deploy_failed", "crash"}
DISCORD_WEBHOOK = re.compile(r"^https://(?:discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)"
                             r"/api/webhooks/\d{5,30}/[A-Za-z0-9_\-]{20,200}\Z")
TITLES = {
    "missing": "서비스를 찾지 못함",
    "spike": "요청 급증", "blocked": "차단 급증", "errors": "오류 증가", "outage": "장애 — 실행 태스크 부족",
    "deploy_failed": "배포 실패", "crash": "컨테이너가 멈춤", "impaired": "태스크를 띄우지 못함", "rollback": "롤백 시작",
}
SUBJECTS = {
    "missing": "service not found",
    "spike": "traffic spike", "blocked": "blocked requests", "errors": "error rate", "outage": "outage",
    "deploy_failed": "deployment failed", "crash": "container stopped", "impaired": "tasks failing to start",
    "rollback": "rollback started",
}

_ser, _des = TypeSerializer(), TypeDeserializer()
_clients: dict = {}


def _client(name: str):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def _env(name: str) -> str:
    return os.environ.get(name, "")


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _item(d: dict) -> dict:
    return {k: _ser.serialize(v) for k, v in d.items() if v is not None}


def _plain(raw: dict | None) -> dict:
    return {k: _des.deserialize(v) for k, v in (raw or {}).items()}


def _get(pk: str, sk: str) -> dict:
    resp = _client("dynamodb").get_item(TableName=_env("WATCH_TABLE"), Key=_item({"pk": pk, "sk": sk}), ConsistentRead=True)
    return _plain(resp.get("Item"))


def _code(exc: Exception) -> str:
    try:
        return str(exc.response["Error"]["Code"])  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return type(exc).__name__


def _rev(arn: str) -> str:
    """태스크 정의 ARN → 'family:3'."""
    return (arn or "").rsplit("/", 1)[-1]


def _revno(arn: str) -> str:
    """태스크 정의 ARN → '3'."""
    return _rev(arn).rsplit(":", 1)[-1]


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def handler(event, context=None):
    if isinstance(event, dict) and isinstance(event.get("requestContext"), dict) and "http" in event["requestContext"]:
        return http(event)
    source = (event or {}).get("source") if isinstance(event, dict) else None
    if source == "aws.cloudwatch":
        return on_alarm(event)
    if source == "aws.ecs":
        return on_ecs(event)
    if source == "aws.events" or (isinstance(event, dict) and event.get("recoder") == "tick"):
        return tick()
    return {"ignored": True}


# ---------------------------------------------------------------------------
# 감지
# ---------------------------------------------------------------------------

def _service() -> dict:
    resp = _client("ecs").describe_services(cluster=_env("WATCH_CLUSTER"), services=[_env("WATCH_SERVICE")])
    return (resp.get("services") or [{}])[0]


def _cooldown_ok(kind: str, now: float) -> bool:
    """같은 종류는 30분에 한 번만 — 원자적으로(두 이벤트가 동시에 와도 한 번)."""
    try:
        _client("dynamodb").update_item(
            TableName=_env("WATCH_TABLE"), Key=_item({"pk": "STATE", "sk": f"COOLDOWN#{kind}"}),
            UpdateExpression="SET #t = :now",
            ConditionExpression="attribute_not_exists(#t) OR #t < :cut",
            ExpressionAttributeNames={"#t": "ts"},
            ExpressionAttributeValues=_item({":now": int(now), ":cut": int(now - COOLDOWNS.get(kind, COOLDOWN_S))}),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        if _code(exc) == "ConditionalCheckFailedException":
            return False
        raise


def raise_event(kind: str, detail: str, *, source: str, severity: str = "high", extra: dict | None = None,
                cooldown: bool = True) -> dict | None:
    now = _now()
    if cooldown and not _cooldown_ok(kind, now):
        return None
    try:
        return _record(kind, detail, now=now, source=source, severity=severity, extra=extra)
    except Exception:
        #: 기록 · 알림 전에 실패하면(표 오류 · 시간 초과) 30분 쉼을 풀어 둔다 — 다시 오는 같은 일을 버리지 않게.
        if cooldown:
            _release_cooldown(kind)
        raise


def _release_cooldown(kind: str) -> None:
    try:
        _client("dynamodb").delete_item(TableName=_env("WATCH_TABLE"), Key=_item({"pk": "STATE", "sk": f"COOLDOWN#{kind}"}))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"cooldown_release": kind, "error": _code(exc)}))


def _record(kind: str, detail: str, *, now: float, source: str, severity: str, extra: dict | None) -> dict:
    config = _get("CONFIG", "CONFIG")
    actions = []
    link = ""
    if kind in ROLLBACK_KINDS:
        offer = _rollback_offer(config, now)
        if offer:
            actions.append(offer["action"])
            link = offer["link"]
    #: 정렬 키 — 마이크로초까지(같은 초에 생긴 일도 순서대로) + 겹치지 않게 짧은 무작위
    sk = f"{datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='microseconds')}#{secrets.token_hex(3)}"
    record = {"pk": "EVENT", "sk": sk, "at": _iso(now), "ts": int(now), "kind": kind, "severity": severity,
              "title": TITLES.get(kind, kind), "detail": detail[:900], "source": source, "actions": actions,
              "ttl": int(now + EVENT_TTL_S), **(extra or {})}
    _client("dynamodb").put_item(TableName=_env("WATCH_TABLE"), Item=_item(record))
    if actions and actions[0].get("token_id"):
        _client("dynamodb").update_item(
            TableName=_env("WATCH_TABLE"), Key=_item({"pk": "TOKEN", "sk": actions[0]["token_id"]}),
            UpdateExpression="SET event_sk = :e", ExpressionAttributeValues=_item({":e": sk}))
    #: 경보가 보낸 일(요청 급증 · 차단 · 오류)은 AWS 경보가 이미 메일을 보냈다 — 롤백 링크가 있을 때만 메일을 한 번 더.
    notify(config, kind, detail, link=link, mail=not (source == "alarm" and not link))
    return record


def _rollback_offer(config: dict, now: float) -> dict | None:
    """이전 버전이 있고 지금과 다르면 롤백 제안을 남긴다(VS Code 모니터에서 승인). 함수 URL 이 있으면 일회용 링크도 —
    링크는 알림에만 실리고 표에는 해시만 남는다."""
    stable, url = str(config.get("stable_td") or ""), str(config.get("url") or "")
    if not stable:
        return None
    try:
        current = str(_service().get("taskDefinition") or "")
    except Exception:  # noqa: BLE001 - 읽지 못하면 링크를 만들지 않는다(엉뚱한 버전에서 되돌리지 않게)
        return None
    if not current or _rev(current) == _rev(stable):
        return None
    expires = int(now + TOKEN_TTL_S)
    action = {"action": "rollback", "label": f"{_rev(stable)} 로 롤백", "target": stable, "from_td": current,
              "expires": expires, "status": "offered"}
    if not url:
        return {"action": action, "link": ""}
    token = secrets.token_urlsafe(32)
    token_id = hashlib.sha256(token.encode()).hexdigest()
    _client("dynamodb").put_item(TableName=_env("WATCH_TABLE"), Item=_item({
        "pk": "TOKEN", "sk": token_id, "action": "rollback", "target": stable, "from_td": current,
        "expires": expires, "status": "pending", "created": int(now), "ttl": expires + 24 * 3600}),
        ConditionExpression="attribute_not_exists(pk)")
    action["token_id"] = token_id
    return {"action": action, "link": url.rstrip("/") + "/r?t=" + token}


def on_alarm(event: dict) -> dict:
    detail = event.get("detail") or {}
    name = str(detail.get("alarmName") or "")
    names = set(filter(None, _env("WATCH_ALARMS").split(",")))
    if name not in names or ((detail.get("state") or {}).get("value") != "ALARM"):
        return {"ignored": True}
    kind = next((k for suffix, k in ALARM_KINDS.items() if name.endswith(suffix)), "")
    if not kind:
        return {"ignored": True}
    reason = str((detail.get("state") or {}).get("reason") or "")
    text = {"spike": "5분 요청 수가 기준을 넘었습니다.", "blocked": "방패가 막은 요청 비율이 20%를 넘었습니다.",
            "errors": "서버 오류(5xx) 비율이 5%를 넘었습니다."}[kind]
    raised = raise_event(kind, text + (f" ({reason[:300]})" if reason else ""), source="alarm")
    return {"raised": bool(raised)}


def _mine(arn: str) -> bool:
    return str(arn).endswith(f"/{_env('WATCH_CLUSTER')}/{_env('WATCH_SERVICE')}")


def on_ecs(event: dict) -> dict:
    kind_name = event.get("detail-type")
    detail = event.get("detail") or {}
    if kind_name == "ECS Task State Change":
        if detail.get("group") != f"service:{_env('WATCH_SERVICE')}" or detail.get("lastStatus") != "STOPPED":
            return {"ignored": True}
        if not str(detail.get("clusterArn", "")).endswith(f"/{_env('WATCH_CLUSTER')}"):
            return {"ignored": True}
        reason = str(detail.get("stoppedReason") or "")
        #: 배포 교체·축소처럼 ECS 가 일부러 멈춘 것은 장애가 아니다 — 단 헬스 체크 실패로 ECS 가 죽인 것은 장애다.
        health = "health check" in reason.lower()
        if detail.get("stopCode") in ("ServiceSchedulerInitiated", "UserInitiated", "SpotInterruption", "TerminationNotice") and not health:
            return {"ignored": True}
        exits = [f"{c.get('name')} 종료 코드 {c.get('exitCode')}" for c in detail.get("containers") or []
                 if c.get("exitCode") not in (None, 0)]
        text = ("헬스 체크에 실패해 ECS 가 태스크를 멈췄습니다" if health else "컨테이너가 멈췄습니다") + (f": {reason[:300]}" if reason else "") + (f" ({', '.join(exits[:3])})" if exits else "")
        raised = raise_event("crash", text, source="ecs", extra={"task_definition": _rev(str(detail.get("taskDefinitionArn") or ""))})
        return {"raised": bool(raised)}
    if kind_name in ("ECS Service Action", "ECS Deployment State Change"):
        if not any(_mine(r) for r in event.get("resources") or []):
            return {"ignored": True}
        name = str(detail.get("eventName") or "")
        if name == "SERVICE_DEPLOYMENT_FAILED":
            raised = raise_event("deploy_failed", str(detail.get("reason") or "ECS 가 새 배포를 실패로 판정했습니다.")[:600],
                                 source="ecs", severity="critical")
        elif name in ("SERVICE_TASK_START_IMPAIRED", "SERVICE_TASK_PLACEMENT_FAILURE"):
            raised = raise_event("impaired", "ECS 가 이 서비스의 태스크를 계속 띄우지 못하고 있습니다"
                                 + (f": {detail.get('reason')}" if detail.get("reason") else "."), source="ecs",
                                 severity="critical")
        else:
            return {"ignored": True}
        return {"raised": bool(raised)}
    return {"ignored": True}


def tick() -> dict:
    """5분마다: 살아 있다는 기록 + 실행 태스크 부족 3번 연속이면 장애."""
    now = _now()
    svc = _service()
    if svc.get("status") != "ACTIVE":
        #: 서비스가 지워졌거나(정리) 내려가는 중 — 장애로 세지 않고, 하루에 한 번만 알린다.
        missing = raise_event("missing", "감시하던 ECS 서비스를 찾지 못했습니다(지워졌거나 비활성). 필요 없으면 VS Code 에서 감시자를 지우세요.",
                              source="tick", severity="high")
        return {"missing": True, "raised": bool(missing)}
    desired, running = int(svc.get("desiredCount") or 0), int(svc.get("runningCount") or 0)
    short = desired > 0 and running < desired
    try:
        #: 같은 일정이 두 번 배달돼도(EventBridge 는 최소 한 번 배달) 4분 안의 두 번째는 세지 않는다.
        resp = _client("dynamodb").update_item(
            TableName=_env("WATCH_TABLE"), Key=_item({"pk": "STATE", "sk": "STATE"}),
            UpdateExpression=("SET last_tick = :now, running = :r, desired = :d, version = :v, task_definition = :td "
                              "ADD ticks :one, strikes :s"),
            ConditionExpression="attribute_not_exists(last_tick) OR last_tick < :gap",
            ExpressionAttributeValues=_item({":now": int(now), ":r": running, ":d": desired, ":v": VERSION, ":gap": int(now - 240),
                                             ":td": str(svc.get("taskDefinition") or ""), ":one": 1, ":s": 1 if short else 0}),
            ReturnValues="ALL_NEW")
    except Exception as exc:  # noqa: BLE001
        if _code(exc) == "ConditionalCheckFailedException":
            return {"running": running, "desired": desired, "raised": False, "duplicate": True}
        raise
    state = _plain(resp.get("Attributes"))
    raised = None
    if not short and int(state.get("strikes") or 0):
        _client("dynamodb").update_item(TableName=_env("WATCH_TABLE"), Key=_item({"pk": "STATE", "sk": "STATE"}),
                                        UpdateExpression="SET strikes = :z", ExpressionAttributeValues=_item({":z": 0}))
    elif short and int(state.get("strikes") or 0) >= OUTAGE_STRIKES:
        raised = raise_event("outage", f"실행 중 {running} / 원하는 수 {desired} 가 {OUTAGE_STRIKES}번 연속(약 15분)입니다.",
                             source="tick", severity="critical")
    return {"running": running, "desired": desired, "raised": bool(raised)}


# ---------------------------------------------------------------------------
# 알림 (메일 · 디스코드)
# ---------------------------------------------------------------------------

def notify(config: dict, kind: str, detail: str, *, link: str = "", mail: bool = True) -> list[str]:
    service = _env("WATCH_SERVICE")
    lines = [f"[ReCoder] {service} — {TITLES.get(kind, kind)}", detail]
    if link:
        lines += ["", "이전 버전으로 되돌리려면(12시간 안에 한 번만 쓸 수 있어요):", link,
                  "코드 수정안은 VS Code 의 ReCoder 모니터에서 검토하세요."]
    else:
        lines += ["", "자세한 내용은 VS Code 의 ReCoder 모니터에서 볼 수 있어요."]
    body = "\n".join(lines)
    sent = []
    topic = str(config.get("topic") or "") if mail else ""
    if topic:
        try:
            _client("sns").publish(TopicArn=topic, Subject=f"[ReCoder] {service[:40]}: {SUBJECTS.get(kind, kind)}"[:99],
                                   Message=body)
            sent.append("mail")
        except Exception as exc:  # noqa: BLE001 - 알림 실패가 기록을 막지 않는다
            print(json.dumps({"notify": "sns", "error": _code(exc)}))
    hook = str(config.get("discord") or "")
    if hook and DISCORD_WEBHOOK.match(hook):
        payload = {"username": "ReCoder", "content": body[:1900], "allowed_mentions": {"parse": []}}
        req = urllib.request.Request(hook, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": f"ReCoder-Watcher/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - 주소는 위 정규식으로 discord.com 만
                resp.read(1)
            sent.append("discord")
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"notify": "discord", "error": type(exc).__name__}))
    return sent


# ---------------------------------------------------------------------------
# 승인 링크 (Lambda 함수 URL)
# ---------------------------------------------------------------------------

_HEADERS = {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "content-security-policy": ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'"),
}


def _page(status: int, title: str, body: str) -> dict:
    doc = ("<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
           "content=\"width=device-width,initial-scale=1\"><meta name=\"robots\" content=\"noindex\">"
           f"<title>{html.escape(title)}</title><style>body{{font:15px/1.6 system-ui,sans-serif;max-width:520px;"
           "margin:40px auto;padding:0 18px;color:#1f2328}}h1{{font-size:20px}}.box{{background:#f6f8fa;border-radius:12px;"
           "padding:14px 16px;margin:16px 0}}button{{font:inherit;font-weight:600;width:100%;height:44px;border:0;"
           "border-radius:10px;background:#0969da;color:#fff}}small{{color:#59636e}}</style></head><body>"
           f"<h1>{html.escape(title)}</h1>{body}</body></html>")
    return {"statusCode": status, "headers": dict(_HEADERS), "body": doc}


def _form(event: dict) -> dict:
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "GET").upper()
    if method == "POST":
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        values = urllib.parse.parse_qs(raw[:4096])
    else:
        values = {k: [v] for k, v in (event.get("queryStringParameters") or {}).items()}
    token = (values.get("t") or [""])[0]
    return {"t": token if re.fullmatch(r"[A-Za-z0-9_\-]{20,100}", token or "") else "",
            "c": (values.get("c") or [""])[0].strip()[:20]}


def http(event: dict) -> dict:
    ctx = (event.get("requestContext") or {}).get("http") or {}
    method, path = str(ctx.get("method", "GET")).upper(), str(ctx.get("path") or event.get("rawPath") or "/")
    if path == "/":
        return {"statusCode": 200, "headers": {"content-type": "text/plain; charset=utf-8", "cache-control": "no-store"},
                "body": "ReCoder watcher"}
    if path != "/r":
        return {"statusCode": 404, "headers": {"content-type": "text/plain; charset=utf-8"}, "body": "not found"}
    if method not in ("GET", "POST"):
        return {"statusCode": 405, "headers": {"content-type": "text/plain; charset=utf-8", "allow": "GET, POST"},
                "body": "method not allowed"}
    form = _form(event)
    token = form["t"]
    if not token:
        return _page(400, "링크가 올바르지 않습니다", "<p>알림에 온 링크를 그대로 여세요.</p>")
    token_id = hashlib.sha256(token.encode()).hexdigest()
    item = _get("TOKEN", token_id)
    now = _now()
    service = html.escape(_env("WATCH_SERVICE"))
    if not item or item.get("status") != "pending" or int(item.get("expires") or 0) <= now:
        why = "이미 사용했거나" if item and item.get("status") != "pending" else "만료됐거나 없는"
        return _page(410, "쓸 수 없는 링크입니다",
                     f"<p>{why} 링크입니다. VS Code 의 ReCoder 모니터에서 지금 상태를 확인하세요.</p>")
    target, from_td = str(item.get("target") or ""), str(item.get("from_td") or "")
    if method == "GET":
        #: 메일 보안 검사기·링크 미리보기가 GET 을 열어도 아무 일도 일어나지 않게 — 실행은 아래 버튼(POST)만.
        left = max(0, int(item.get("expires", 0)) - int(now)) // 60
        body = (f"<div class=\"box\"><b>{service}</b><br>지금 버전 {html.escape(_rev(from_td))} → "
                f"<b>{html.escape(_rev(target))}</b> 으로 되돌립니다.<br><small>이전 이미지로 무중단 교체 · 1~3분 · "
                f"{left // 60}시간 {left % 60}분 뒤 만료 · 한 번만 쓸 수 있음</small></div>"
                f"<form method=\"post\" action=\"r\"><input type=\"hidden\" name=\"t\" value=\"{html.escape(token)}\">"
                f"<label>확인을 위해 되돌릴 버전 번호 <b>{html.escape(_revno(target))}</b> 를 입력하세요<br>"
                "<input name=\"c\" required inputmode=\"numeric\" autocomplete=\"off\" "
                "style=\"font:inherit;width:100%;height:40px;margin:8px 0 12px;padding:0 10px;border:1px solid #d0d7de;border-radius:8px\"></label>"
                f"<button type=\"submit\">{html.escape(_rev(target))} 으로 롤백 승인</button></form>"
                "<p><small>코드 수정은 VS Code 에서 검토하세요. 이 링크는 이 롤백 하나만 할 수 있습니다.</small></p>")
        return _page(200, "롤백 승인", body)
    #: 메일 보안 검사기가 양식을 대신 보내도 실행되지 않게 — 사람이 버전 번호를 직접 입력해야 한다.
    if form["c"] != _revno(target):
        return _page(400, "버전 번호가 맞지 않습니다", "<p>링크를 다시 열어 되돌릴 버전 번호를 그대로 입력하세요. 링크는 아직 쓸 수 있습니다.</p>")
    return _approve(token_id, target, from_td, now)


def _approve(token_id: str, target: str, from_td: str, now: float) -> dict:
    ddb = _client("dynamodb")
    try:
        ddb.update_item(
            TableName=_env("WATCH_TABLE"), Key=_item({"pk": "TOKEN", "sk": token_id}),
            UpdateExpression="SET #s = :used, used_at = :now",
            ConditionExpression="#s = :pending AND expires > :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=_item({":used": "used", ":pending": "pending", ":now": int(now)}))
    except Exception as exc:  # noqa: BLE001
        if _code(exc) == "ConditionalCheckFailedException":
            return _page(410, "쓸 수 없는 링크입니다", "<p>이미 사용했거나 만료된 링크입니다.</p>")
        raise
    svc = _service()
    current = str(svc.get("taskDefinition") or "")
    service = _env("WATCH_SERVICE")
    if _rev(current) != _rev(from_td):
        _mark(token_id, "stale")
        raise_event("rollback", f"롤백 링크를 열었지만 그 사이 버전이 바뀌어({_rev(current)}) 롤백하지 않았습니다.",
                    source="link", severity="info", cooldown=False)
        return _page(409, "롤백하지 않았습니다",
                     f"<p>링크를 만든 뒤 {html.escape(service)} 의 버전이 바뀌었습니다(지금 {html.escape(_rev(current))}). "
                     "엉뚱한 버전을 되돌리지 않도록 멈췄습니다. VS Code 에서 확인하세요.</p>")
    try:
        _client("ecs").update_service(cluster=_env("WATCH_CLUSTER"), service=service, taskDefinition=target,
                                      forceNewDeployment=True)
    except Exception as exc:  # noqa: BLE001
        _mark(token_id, "failed")
        raise_event("rollback", f"롤백을 시작하지 못했습니다: {_code(exc)}", source="link", severity="high", cooldown=False)
        return _page(502, "롤백을 시작하지 못했습니다",
                     f"<p>AWS 가 거절했습니다({html.escape(_code(exc))}). VS Code 의 ReCoder 에서 다시 시도하세요.</p>")
    _event_action(token_id, "used")
    raise_event("rollback", f"승인 링크로 {_rev(from_td)} → {_rev(target)} 롤백을 시작했습니다.", source="link",
                severity="info", cooldown=False, extra={"target": target, "from_td": from_td})
    return _page(200, "롤백을 시작했습니다",
                 f"<div class=\"box\"><b>{html.escape(service)}</b> 을 {html.escape(_rev(target))} 으로 되돌리는 중입니다. "
                 "1~3분 뒤 반영됩니다.</div><p><small>원인 코드는 VS Code 의 ReCoder 모니터에서 확인하세요.</small></p>")


def _mark(token_id: str, status: str) -> None:
    _client("dynamodb").update_item(
        TableName=_env("WATCH_TABLE"), Key=_item({"pk": "TOKEN", "sk": token_id}),
        UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=_item({":s": status}))
    _event_action(token_id, status)


def _event_action(token_id: str, status: str) -> None:
    """롤백 제안을 실은 기록(EVENT)에도 결과를 남긴다 — 모니터가 '승인 대기'에서 빼도록."""
    event_sk = str(_get("TOKEN", token_id).get("event_sk") or "")
    if not event_sk:
        return
    try:
        _client("dynamodb").update_item(
            TableName=_env("WATCH_TABLE"), Key=_item({"pk": "EVENT", "sk": event_sk}),
            UpdateExpression="SET actions[0].#s = :s", ConditionExpression="attribute_exists(actions)",
            ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues=_item({":s": status}))
    except Exception as exc:  # noqa: BLE001 - 기록 표시는 롤백 결과를 바꾸지 않는다
        print(json.dumps({"event_action": status, "error": _code(exc)}))
