"""24시간 감시자 설치 · 상태 · 기록 · 롤백 (2.0.48).

감시자는 사용자 AWS 계정 안에서 도는 작은 Lambda 다(코드: core/watcher_lambda/handler.py).
PC 가 꺼져 있어도 경보 · ECS 이벤트 · 5분 일정으로 깨어나 기록하고 알리고, 이전 버전 롤백 링크를 준비한다.
이 모듈은 VS Code 쪽(코어)에서 그것을 설치 · 삭제하고, 기록을 읽고, 사람이 승인한 롤백을 실행한다.

만드는 것(서비스마다, 이름은 모두 recoder- 로 시작 · recoder:managed 태그)
    · DynamoDB 표 1개(기록 · 승인 링크 · 상태 — 30일 뒤 자동 삭제, 요청 수만큼 과금)
    · Lambda 1개 + 함수 URL(승인 링크용) + 로그 그룹(14일 보관)
    · EventBridge 규칙 4개(경보 · 태스크 멈춤 · 서비스 이상 · 5분 일정)
    · ECS 서비스에 recoder:watch=true 태그(감시자 역할은 이 태그가 붙은 서비스만 되돌릴 수 있다)

역할은 만들지 않는다. 배포 키가 IAM 역할을 만들 수 있으면 아무 권한의 역할을 만들어 Lambda 에 넘기는 권한 상승
경로가 생긴다. 그래서 감시자 역할(recoder-watcher)은 원클릭 IAM 셋업(CloudFormation)이나 사람이 콘솔에서 한 번
만들고, 배포 키는 그 역할을 Lambda 에 넘기기(PassRole)만 한다. 학교 계정은 LabRole 을 쓴다.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
except ImportError:  # pragma: no cover - boto3 는 코어 의존성
    TypeDeserializer = TypeSerializer = None  # type: ignore

try:
    import ops_alerts  # type: ignore
    import aws_infra  # type: ignore
    import aws_policy  # type: ignore
except ImportError:  # pragma: no cover
    from core import ops_alerts, aws_infra, aws_policy  # type: ignore

logger = logging.getLogger(__name__)

VERSION = "2.0.48"
ROLE_NAME = aws_policy.WATCHER_ROLE
RUNTIME = "python3.13"
def _handler_file() -> Path:
    """소스 실행 · 실행 파일(PyInstaller) · core.* 이름 모두에서 같은 파일(resources 참고)."""
    try:
        from resources import resource  # type: ignore
    except ImportError:  # pragma: no cover
        try:
            from core.resources import resource  # type: ignore
        except ImportError:
            return Path(__file__).resolve().parent / "watcher_lambda" / "handler.py"
    return resource("watcher_lambda", "handler.py")
WATCH_TAG = "recoder:watch"
LOG_RETENTION_DAYS = 14
RESERVED_CONCURRENCY = 5
TICK = "rate(5 minutes)"
ALARM_SUFFIXES = ("traffic-spike", "blocked-ratio", "error-ratio")
DISCORD_WEBHOOK = re.compile(r"^https://(?:discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)"
                             r"/api/webhooks/\d{5,30}/[A-Za-z0-9_\-]{20,200}\Z")
_ROLE_ARN = re.compile(r"^arn:aws(?:-[a-z]+)*:iam::\d{12}:role/(?:[\w+=,.@-]+/)*[\w+=,.@-]{1,64}$")
#: 감시자 한 달 예상 요금(추가분). 경보는 감지 알림(2.0.25)이 이미 만든 것이라 셈하지 않는다.
MONTHLY_COST_NOTE = "월 약 $0.1 미만 · 기록 표 쓰기 · 로그 · Lambda 무료 사용량 안"


class WatchError(Exception):
    """사람에게 보여 줄 수 있는 설치 · 실행 오류."""


class _ClientMap:
    """배포 파이프라인의 클라이언트 묶음(dict)을 세션처럼 쓴다 — 같은 자격증명으로."""

    def __init__(self, clients: dict) -> None:
        self._clients = clients

    def client(self, name: str, **_: Any) -> Any:
        return self._clients[name]


def from_clients(clients: dict) -> Any:
    return _ClientMap(clients)


# ---------------------------------------------------------------------------
# 이름
# ---------------------------------------------------------------------------

def names(region: str, cluster: str, service: str) -> dict:
    """서비스마다 고정된 이름. 서비스 이름이 길어도 64자(Lambda · 규칙 한도) 안에 들어가고, 서로 겹치지 않는다."""
    safe = re.sub(r"[^A-Za-z0-9-]+", "-", service)[:28].strip("-") or "app"
    digest = hashlib.sha1(f"{region}/{cluster}/{service}".encode()).hexdigest()[:6]
    base = f"recoder-{safe}-{digest}"
    fn = f"{base}-watch"
    return {
        "base": base, "function": fn, "table": fn, "log_group": f"/aws/lambda/{fn}",
        "rules": {"alarm": f"{base}-w-alarm", "task": f"{base}-w-task", "svc": f"{base}-w-svc", "tick": f"{base}-w-tick"},
        "alarms": [ops_alerts._name(service, s) for s in ALARM_SUFFIXES],
        "topic": ops_alerts._name(service, "alerts"),
    }


def _code(exc: Exception) -> str:
    return aws_infra.error_code(exc)


def _msg(exc: Exception) -> str:
    return aws_infra.error_message(exc)


def _missing(exc: Exception) -> bool:
    return _code(exc) in ("ResourceNotFoundException", "ResourceNotFound", "NoSuchEntity", "NotFoundException")


# ---------------------------------------------------------------------------
# Lambda 코드 묶음
# ---------------------------------------------------------------------------

def package() -> bytes:
    """handler.py 한 파일을 zip 으로. 날짜 · 권한을 고정해 같은 코드면 같은 해시가 나온다(바뀔 때만 올린다)."""
    buf = io.BytesIO()
    info = zipfile.ZipInfo("handler.py", date_time=(1980, 1, 1, 0, 0, 0))
    info.external_attr = 0o644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(info, _handler_file().read_bytes())
    return buf.getvalue()


def code_sha256(blob: bytes) -> str:
    import base64
    return base64.b64encode(hashlib.sha256(blob).digest()).decode()


# ---------------------------------------------------------------------------
# DynamoDB 값 변환
# ---------------------------------------------------------------------------

def _ser(d: dict) -> dict:
    s = TypeSerializer()
    return {k: s.serialize(v) for k, v in d.items() if v is not None}


def _plain(value: Any) -> Any:
    from decimal import Decimal
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _des(raw: Optional[dict]) -> dict:
    d = TypeDeserializer()
    return _plain({k: d.deserialize(v) for k, v in (raw or {}).items()})


# ---------------------------------------------------------------------------
# 확인 · 계획
# ---------------------------------------------------------------------------

def _account(session: Any) -> str:
    return str(session.client("sts").get_caller_identity()["Account"])


def role_arn_for(session: Any, region: str, role: str = "") -> str:
    role = (role or "").strip()
    if role.startswith("arn:"):
        if not _ROLE_ARN.match(role):
            raise WatchError("역할 ARN 모양이 아닙니다(arn:aws:iam::123456789012:role/이름).")
        return role
    #: 학교 계정(AWS Academy)은 역할을 만들 수 없어 실행 역할과 같은 LabRole 을 쓴다 — 권한표도 같은 규칙(aws_policy).
    name = role or aws_policy.watcher_role_for(aws_policy.configured_execution_role())
    if not re.fullmatch(r"[\w+=,.@-]{1,64}", name):
        raise WatchError("역할 이름에 쓸 수 없는 글자가 있습니다.")
    return f"arn:{aws_policy.partition_for(region)}:iam::{_account(session)}:role/{name}"


def _service(session: Any, cluster: str, service: str) -> dict:
    svc = (session.client("ecs").describe_services(cluster=cluster, services=[service]).get("services") or [{}])[0]
    if not svc.get("serviceArn") or svc.get("status") != "ACTIVE":
        raise WatchError(f"{service} 서비스를 찾지 못했습니다(지워졌거나 다른 리전).")
    return svc


def plan(session: Any, *, region: str, cluster: str, service: str, role: str = "") -> dict:
    """설치하면 무엇이 생기는지 · 역할이 준비됐는지(승인 카드에 그대로 보인다). AWS 에 아무것도 만들지 않는다."""
    n = names(region, cluster, service)
    svc = _service(session, cluster, service)
    arn = role_arn_for(session, region, role)
    role_state: Optional[bool]
    try:
        session.client("iam").get_role(RoleName=arn.rsplit("/", 1)[-1])
        role_state = True
    except Exception as exc:  # noqa: BLE001
        role_state = False if _code(exc) == "NoSuchEntity" else None
    notes = []
    if role_state is False:
        notes.append(f"상시 감시 역할 {arn.rsplit('/', 1)[-1]} 이 없습니다 — AWS 연결 화면의 「원클릭 IAM 셋업」 스택을 최신으로 "
                     "업데이트하거나, 권한 안내의 상시 감시 역할을 만들어 주세요(학교 계정은 LabRole).")
    elif role_state is None:
        notes.append("역할이 있는지 확인하지 못했습니다(iam:GetRole 권한 없음) — 설치할 때 확인합니다.")
    return {
        "service": service, "cluster": cluster, "region": region, "revision": _rev(svc.get("taskDefinition", "")),
        "role": {"arn": arn, "exists": role_state},
        "resources": [
            {"kind": "Lambda", "name": n["function"], "detail": "경보 · ECS 이벤트 · 5분마다 깨어남 · 승인 링크"},
            {"kind": "DynamoDB", "name": n["table"], "detail": "기록 · 승인 링크 · 상태(30일 뒤 자동 삭제)"},
            {"kind": "EventBridge", "name": f"{n['base']}-w-*", "detail": "규칙 4개(경보 · 태스크 멈춤 · 서비스 이상 · 5분)"},
            {"kind": "CloudWatch Logs", "name": n["log_group"], "detail": f"{LOG_RETENTION_DAYS}일 보관"},
        ],
        "permissions": "읽기 · 기록 · 알림 · 승인된 롤백만(recoder:watch 태그가 붙은 서비스)",
        "cost": MONTHLY_COST_NOTE,
        "notes": notes,
    }


def _rev(arn: str) -> str:
    return (arn or "").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# 설치
# ---------------------------------------------------------------------------

def _retry(fn: Callable[[], Any], codes: tuple[str, ...], *, attempts: int, delay: float,
           sleep: Callable[[float], None], match: str = "") -> Any:
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            retry = _code(exc) in codes and (not match or match in str(exc))
            if not retry or i == attempts - 1:
                raise
            sleep(delay)
    return None


def install(session: Any, *, region: str, cluster: str, service: str, role: str = "", discord: str = "",
            stable_td: str = "", approved: bool = False, sleep: Callable[[float], None] = time.sleep) -> dict:
    """승인한 뒤에만. 몇 번을 다시 실행해도 같은 결과(있으면 갱신)."""
    if not approved:
        raise PermissionError("승인 없이 설치하지 않습니다.")
    discord = (discord or "").strip()
    if discord and not DISCORD_WEBHOOK.match(discord):
        raise WatchError("디스코드 웹후크 주소가 아닙니다(https://discord.com/api/webhooks/… 형식).")
    n = names(region, cluster, service)
    svc = _service(session, cluster, service)
    role_arn = role_arn_for(session, region, role)
    tags = aws_infra.recoder_tags("", service)
    notes: list[str] = []
    ddb = session.client("dynamodb")
    lam = session.client("lambda")
    events = session.client("events")
    logs = session.client("logs")

    # 1. 기록 표
    try:
        ddb.create_table(TableName=n["table"], BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                               {"AttributeName": "sk", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                         Tags=[{"Key": k, "Value": v} for k, v in tags.items()])
    except Exception as exc:  # noqa: BLE001
        if _code(exc) != "ResourceInUseException":
            raise WatchError(f"기록 표를 만들지 못했습니다: {_msg(exc)}") from exc
    ddb.get_waiter("table_exists").wait(TableName=n["table"], WaiterConfig={"Delay": 2, "MaxAttempts": 60})
    try:
        ddb.update_time_to_live(TableName=n["table"], TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})
    except Exception as exc:  # noqa: BLE001 - 이미 켜져 있으면 ValidationException
        if _code(exc) != "ValidationException":
            notes.append(f"기록 자동 삭제(TTL)를 켜지 못했습니다: {_msg(exc)}")

    # 2. 로그 그룹(보관 기간을 먼저 정해 둔다 — Lambda 가 알아서 만들면 영구 보관이 된다)
    try:
        logs.create_log_group(logGroupName=n["log_group"], tags=tags)
    except Exception as exc:  # noqa: BLE001
        if _code(exc) != "ResourceAlreadyExistsException":
            notes.append(f"로그 그룹을 미리 만들지 못했습니다: {_msg(exc)}")
    try:
        logs.put_retention_policy(logGroupName=n["log_group"], retentionInDays=LOG_RETENTION_DAYS)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"로그 보관 기간을 정하지 못했습니다: {_msg(exc)}")

    # 3. 알림 주제(감지 알림이 이미 만들었으면 같은 주제 — create_topic 은 같은 이름이면 그대로 돌려준다)
    topic = ""
    try:
        topic = str(session.client("sns").create_topic(
            Name=n["topic"], Tags=[{"Key": k, "Value": v} for k, v in tags.items()])["TopicArn"])
    except Exception as exc:  # noqa: BLE001
        notes.append(f"메일 알림 주제를 준비하지 못했습니다: {_msg(exc)} — 디스코드 알림과 VS Code 의 Operate 화면만 씁니다.")

    # 4. Lambda
    blob = package()
    env = {"Variables": {"WATCH_TABLE": n["table"], "WATCH_CLUSTER": cluster, "WATCH_SERVICE": service,
                         "WATCH_ALARMS": ",".join(n["alarms"])}}
    try:
        current = lam.get_function(FunctionName=n["function"])["Configuration"]
    except Exception as exc:  # noqa: BLE001
        if not _missing(exc):
            raise WatchError(f"상시 감시 함수를 확인하지 못했습니다: {_msg(exc)}") from exc
        current = None
    try:
        if current is None:
            #: 방금 만든 역할은 IAM 전파에 수십 초 걸린다 — "역할을 맡을 수 없다"면 잠깐 기다렸다 다시.
            fn_arn = _retry(lambda: lam.create_function(
                FunctionName=n["function"], Runtime=RUNTIME, Role=role_arn, Handler="handler.handler",
                Code={"ZipFile": blob}, Timeout=15, MemorySize=128, Environment=env, Tags=tags,
                Description=f"ReCoder 24h watcher - {service}"), ("InvalidParameterValueException",),
                attempts=8, delay=5, sleep=sleep, match="role")["FunctionArn"]
            lam.get_waiter("function_active_v2").wait(FunctionName=n["function"], WaiterConfig={"Delay": 2, "MaxAttempts": 60})
        else:
            fn_arn = current["FunctionArn"]
            if current.get("CodeSha256") != code_sha256(blob):
                lam.update_function_code(FunctionName=n["function"], ZipFile=blob)
                lam.get_waiter("function_updated_v2").wait(FunctionName=n["function"], WaiterConfig={"Delay": 2, "MaxAttempts": 60})
            lam.update_function_configuration(FunctionName=n["function"], Role=role_arn, Environment=env,
                                              Runtime=RUNTIME, Timeout=15, MemorySize=128)
            lam.get_waiter("function_updated_v2").wait(FunctionName=n["function"], WaiterConfig={"Delay": 2, "MaxAttempts": 60})
    except WatchError:
        raise
    except Exception as exc:  # noqa: BLE001
        if _code(exc) in ("AccessDeniedException", "AccessDenied") and "PassRole" in str(exc):
            raise WatchError(f"상시 감시 역할을 Lambda 에 넘길 권한이 없습니다(iam:PassRole · {role_arn.rsplit('/', 1)[-1]}) — "
                             "권한 템플릿을 최신으로 다시 적용하세요.") from exc
        if _code(exc) == "InvalidParameterValueException" and "role" in str(exc).lower():
            raise WatchError(f"Lambda 가 역할 {role_arn.rsplit('/', 1)[-1]} 을 맡을 수 없습니다 — 역할이 없거나 "
                             "신뢰 관계에 lambda.amazonaws.com 이 없습니다.") from exc
        raise WatchError(f"상시 감시 함수를 만들지 못했습니다: {_msg(exc)}") from exc

    # 5. 승인 링크(함수 URL) — 조직 정책이 공개 URL 을 막으면 링크 없이 설치한다(승인은 VS Code 에서).
    url = ""
    try:
        try:
            url = str(lam.get_function_url_config(FunctionName=n["function"])["FunctionUrl"])
        except Exception as exc:  # noqa: BLE001
            if not _missing(exc):
                raise
            url = str(lam.create_function_url_config(FunctionName=n["function"], AuthType="NONE")["FunctionUrl"])
        for sid, kwargs in (("recoder-url-public", {"Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "NONE"}),
                            ("recoder-url-invoke", {"Action": "lambda:InvokeFunction", "InvokedViaFunctionUrl": True})):
            try:
                lam.add_permission(FunctionName=n["function"], StatementId=sid, Principal="*", **kwargs)
            except Exception as exc:  # noqa: BLE001
                if _code(exc) != "ResourceConflictException":
                    raise
    except Exception as exc:  # noqa: BLE001
        url = ""
        notes.append(f"롤백 승인 링크를 만들지 못했습니다({_msg(exc)}) — 롤백 승인은 VS Code 의 Operate 화면에서 합니다.")

    # 6. 깨우는 규칙
    patterns = {
        "alarm": {"source": ["aws.cloudwatch"], "detail-type": ["CloudWatch Alarm State Change"],
                  "detail": {"alarmName": n["alarms"], "state": {"value": ["ALARM"]}}},
        "task": {"source": ["aws.ecs"], "detail-type": ["ECS Task State Change"],
                 "detail": {"clusterArn": [svc["clusterArn"]], "group": [f"service:{service}"], "lastStatus": ["STOPPED"]}},
        "svc": {"source": ["aws.ecs"], "detail-type": ["ECS Service Action", "ECS Deployment State Change"],
                "resources": [svc["serviceArn"]]},
    }
    import json as _json
    for key, rule in n["rules"].items():
        spec = {"ScheduleExpression": TICK} if key == "tick" else {"EventPattern": _json.dumps(patterns[key])}
        try:
            rule_arn = events.put_rule(Name=rule, State="ENABLED", Description=f"ReCoder watcher - {service}",
                                       Tags=[{"Key": k, "Value": v} for k, v in tags.items()], **spec)["RuleArn"]
            events.put_targets(Rule=rule, Targets=[{"Id": "watch", "Arn": fn_arn}])
            try:
                lam.add_permission(FunctionName=n["function"], StatementId=f"recoder-{key}", Action="lambda:InvokeFunction",
                                   Principal="events.amazonaws.com", SourceArn=rule_arn)
            except Exception as exc:  # noqa: BLE001
                if _code(exc) != "ResourceConflictException":
                    raise
        except Exception as exc:  # noqa: BLE001
            raise WatchError(f"상시 감시를 깨우는 규칙({key})을 만들지 못했습니다: {_msg(exc)}") from exc

    # 7. 설정(알림 주소 · 롤백 기준 버전) — 이미 있던 디스코드 주소 · 기준 버전은 비워 두면 그대로
    sets = {"service": service, "cluster": cluster, "region": region, "url": url, "topic": topic, "version": VERSION,
            "installed_at": int(time.time()), "role": role_arn}
    if discord:
        sets["discord"] = discord
    if stable_td:
        sets["stable_td"] = stable_td
    _update_config(ddb, n["table"], sets)

    #: 경보의 직접 메일(2.0.25)은 그대로 둔다 — 감시자가 멈춰도 메일은 오게(안전망). 감시자는 경보로 생긴 일에는
    #: 롤백 링크가 있을 때만 메일을 한 번 더 보낸다(handler.notify).

    # 8. 공개 승인 링크에 몰려도 계정의 다른 Lambda 를 굶기지 않게 동시 실행을 묶는다(계정 한도가 작으면 생략).
    try:
        lam.put_function_concurrency(FunctionName=n["function"], ReservedConcurrentExecutions=RESERVED_CONCURRENCY)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"상시 감시 동시 실행 한도를 정하지 못했습니다({_code(exc)}) — 계정의 Lambda 동시 실행 한도가 작으면 생깁니다(동작에는 문제 없음).")

    # 9. 감시자 역할이 이 서비스만 되돌릴 수 있게 표시
    try:
        session.client("ecs").tag_resource(resourceArn=svc["serviceArn"], tags=[{"key": WATCH_TAG, "value": "true"}])
    except Exception as exc:  # noqa: BLE001
        notes.append(f"서비스에 감시 표시를 붙이지 못했습니다({_msg(exc)}) — 롤백 승인 링크가 권한 부족으로 실패할 수 있습니다.")

    return {"installed": True, "function": n["function"], "url": bool(url), "notes": notes,
            "message": "상시 감시를 설치했습니다 — 5분 안에 첫 확인 기록이 남습니다."}


def _update_config(ddb: Any, table: str, sets: dict, *, must_exist: bool = False) -> None:
    keys = list(sets)
    kwargs: dict[str, Any] = {
        "TableName": table, "Key": _ser({"pk": "CONFIG", "sk": "CONFIG"}),
        "UpdateExpression": "SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(keys))),
        "ExpressionAttributeNames": {f"#k{i}": k for i, k in enumerate(keys)},
        "ExpressionAttributeValues": _ser({f":v{i}": sets[k] for i, k in enumerate(keys)}),
    }
    if must_exist:
        kwargs["ConditionExpression"] = "attribute_exists(pk)"
    ddb.update_item(**kwargs)


# ---------------------------------------------------------------------------
# 상태 · 기록
# ---------------------------------------------------------------------------

def status(session: Any, *, region: str, cluster: str, service: str, now: Optional[float] = None) -> dict:
    n = names(region, cluster, service)
    ddb = session.client("dynamodb")
    try:
        config = _des(ddb.get_item(TableName=n["table"], Key=_ser({"pk": "CONFIG", "sk": "CONFIG"}),
                                   ConsistentRead=True).get("Item"))
        state = _des(ddb.get_item(TableName=n["table"], Key=_ser({"pk": "STATE", "sk": "STATE"}),
                                  ConsistentRead=True).get("Item"))
    except Exception as exc:  # noqa: BLE001
        if _missing(exc):
            return {"installed": False}
        return {"installed": None, "error": _msg(exc)}
    if not config:
        return {"installed": False}
    now = now or time.time()
    last = int(state.get("last_tick") or 0)
    return {
        "installed": True, "version": config.get("version", ""), "approval_link": bool(config.get("url")),
        "discord": bool(config.get("discord")), "mail": bool(config.get("topic")),
        "stable": _rev(str(config.get("stable_td") or "")),
        "last_tick": last, "ticks": int(state.get("ticks") or 0),
        #: 5분 일정이 두 번 넘게 비면 감시자가 멈춘 것(역할 권한이 빠졌거나 규칙이 꺼짐)
        "healthy": bool(last) and now - last < 15 * 60,
        "waiting_first_tick": not last,
        "running": state.get("running"), "desired": state.get("desired"),
        "installed_at": int(config.get("installed_at") or 0),
    }


def events(session: Any, *, region: str, cluster: str, service: str, since: str = "", limit: int = 100) -> list[dict]:
    """최근 기록(새 것 먼저). since 는 ISO 시각 — 그보다 뒤 것만."""
    n = names(region, cluster, service)
    kwargs: dict[str, Any] = {"TableName": n["table"], "ScanIndexForward": False, "Limit": max(1, min(200, int(limit)))}
    if since:
        kwargs.update(KeyConditionExpression="pk = :p AND sk > :s", ExpressionAttributeValues=_ser({":p": "EVENT", ":s": since}))
    else:
        kwargs.update(KeyConditionExpression="pk = :p", ExpressionAttributeValues=_ser({":p": "EVENT"}))
    try:
        items = session.client("dynamodb").query(**kwargs).get("Items") or []
    except Exception as exc:  # noqa: BLE001
        if _missing(exc):
            return []
        raise
    out = []
    for raw in items:
        e = _des(raw)
        for a in e.get("actions") or []:
            a.pop("token_id", None)  # 링크 토큰의 해시도 화면으로 보내지 않는다
        out.append({k: e[k] for k in ("sk", "at", "ts", "kind", "severity", "title", "detail", "source", "actions",
                                      "target", "from_td", "task_definition") if k in e})
    return out


def offers(evts: list[dict], current_td: str, now: Optional[float] = None) -> list[dict]:
    """아직 쓸 수 있는 롤백 제안 — 만료 전 · 아직 쓰지 않음 · 그 뒤로 버전이 바뀌지 않음. 같은 대상은 하나만."""
    now = now or time.time()
    seen, out = set(), []
    for e in evts:
        for a in e.get("actions") or []:
            if (a.get("action") == "rollback" and a.get("status") == "offered" and int(a.get("expires") or 0) > now
                    and _rev(str(a.get("from_td") or "")) == _rev(current_td) and a.get("target") and a["target"] not in seen):
                seen.add(a["target"])
                out.append({"event": e.get("sk"), "title": e.get("title"), "at": e.get("at"), "target": a["target"],
                            "from_td": a["from_td"], "label": a.get("label") or f"{_rev(a['target'])} 로 롤백",
                            "expires": int(a.get("expires") or 0)})
    return out


def set_versions(session: Any, *, region: str, cluster: str, service: str, current_td: str, stable_td: str) -> bool:
    """배포가 끝날 때마다(코어) — 롤백 기준 버전을 바꾼다. 감시자가 없으면 아무것도 안 한다."""
    if not stable_td or _rev(stable_td) == _rev(current_td):
        return False
    n = names(region, cluster, service)
    try:
        _update_config(session.client("dynamodb"), n["table"], {"stable_td": stable_td, "current_td": current_td},
                       must_exist=True)
        return True
    except Exception as exc:  # noqa: BLE001
        if _missing(exc) or _code(exc) == "ConditionalCheckFailedException":
            return False
        logger.info("감시자 기준 버전을 바꾸지 못했습니다: %s", _msg(exc))
        return False


def settings(session: Any, *, region: str, cluster: str, service: str, discord: Optional[str] = None,
             email: str = "") -> dict:
    n = names(region, cluster, service)
    notes = []
    if discord is not None:
        discord = discord.strip()
        if discord and not DISCORD_WEBHOOK.match(discord):
            raise WatchError("디스코드 웹후크 주소가 아닙니다(https://discord.com/api/webhooks/… 형식).")
        try:
            _update_config(session.client("dynamodb"), n["table"], {"discord": discord}, must_exist=True)
        except Exception as exc:  # noqa: BLE001
            if _missing(exc) or _code(exc) == "ConditionalCheckFailedException":
                raise WatchError("상시 감시가 설치돼 있지 않습니다.") from exc
            raise
        notes.append("디스코드 알림을 껐습니다." if not discord else "디스코드 알림 주소를 저장했습니다.")
    email = (email or "").strip()
    if email:
        if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}", email):
            raise WatchError("메일 주소 모양이 아닙니다.")
        sns = session.client("sns")
        topic = str(sns.create_topic(Name=n["topic"])["TopicArn"])
        sns.subscribe(TopicArn=topic, Protocol="email", Endpoint=email)
        try:  # 설치 때 주제를 못 만들었으면 감시자가 이제 이 주제로 보내게
            _update_config(session.client("dynamodb"), n["table"], {"topic": topic}, must_exist=True)
        except Exception as exc:  # noqa: BLE001
            if not (_missing(exc) or _code(exc) == "ConditionalCheckFailedException"):
                raise
        notes.append(f"{email} 로 구독 확인 메일을 보냈습니다 — 메일의 확인 링크를 눌러야 받습니다.")
    return {"ok": True, "message": " ".join(notes) or "바뀐 것이 없습니다."}


# ---------------------------------------------------------------------------
# VS Code 에서 승인한 롤백
# ---------------------------------------------------------------------------

def rollback(session: Any, *, region: str, cluster: str, service: str, target_td: str, from_td: str,
             approved: bool) -> dict:
    """사람이 모니터에서 승인한 롤백. 그 사이 버전이 바뀌었으면 하지 않는다(엉뚱한 버전을 되돌리지 않게)."""
    if not approved:
        raise PermissionError("승인 없이 되돌리지 않습니다.")
    if not target_td or _rev(target_td) == _rev(from_td):
        raise WatchError("되돌릴 버전이 없습니다.")
    n = names(region, cluster, service)
    try:
        config = _des(session.client("dynamodb").get_item(TableName=n["table"], Key=_ser({"pk": "CONFIG", "sk": "CONFIG"}),
                                                          ConsistentRead=True).get("Item"))
    except Exception as exc:  # noqa: BLE001
        if not _missing(exc):
            raise
        config = {}
    #: 감시자가 제안한 대상(이번 배포 직전 버전)으로만 — 임의의 옛 버전으로 되돌리는 통로가 되지 않게.
    if not config.get("stable_td") or _rev(str(config["stable_td"])) != _rev(target_td):
        raise WatchError("상시 감시가 제안한 이전 버전이 아닙니다 — Operate 화면을 새로 고친 뒤 다시 확인하세요.")
    svc = _service(session, cluster, service)
    current = str(svc.get("taskDefinition") or "")
    if _rev(current) != _rev(from_td):
        raise WatchError(f"그 사이 버전이 바뀌었습니다(지금 {_rev(current)}) — Operate 화면을 새로 고친 뒤 다시 확인하세요.")
    session.client("ecs").update_service(cluster=cluster, service=service, taskDefinition=target_td, forceNewDeployment=True)
    now = time.time()
    try:
        session.client("dynamodb").put_item(TableName=n["table"], Item=_ser({
            "pk": "EVENT", "sk": f"{datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='microseconds')}#vscode",
            "at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"), "ts": int(now),
            "kind": "rollback", "severity": "info", "title": "롤백 시작",
            "detail": f"VS Code 에서 승인 — {_rev(from_td)} → {_rev(target_td)}", "source": "vscode", "actions": [],
            "target": target_td, "from_td": from_td, "ttl": int(now + 30 * 24 * 3600)}))
    except Exception as exc:  # noqa: BLE001 - 감시자가 없으면 기록만 빠진다
        if not _missing(exc):
            logger.info("롤백 기록을 남기지 못했습니다: %s", _msg(exc))
    return {"ok": True, "message": f"{_rev(from_td)} → {_rev(target_td)} 롤백을 시작했습니다 — 1~3분 뒤 반영됩니다."}


# ---------------------------------------------------------------------------
# 삭제
# ---------------------------------------------------------------------------

def uninstall(session: Any, *, region: str, cluster: str, service: str, approved: bool) -> dict:
    """감시자가 만든 것을 모두 지운다. 하나가 실패해도 나머지는 계속하고, 무엇이 남았는지 알려 준다."""
    if not approved:
        raise PermissionError("승인 없이 지우지 않습니다.")
    n = names(region, cluster, service)
    results: list[dict] = []

    def step(label: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            results.append({"label": label, "ok": True})
        except Exception as exc:  # noqa: BLE001
            if _missing(exc):
                results.append({"label": label, "ok": True, "note": "이미 없음"})
            else:
                results.append({"label": label, "ok": False, "note": _msg(exc)})

    events_c = session.client("events")
    lam = session.client("lambda")
    for key, rule in n["rules"].items():
        step(f"규칙 {key}", lambda rule=rule: (events_c.remove_targets(Rule=rule, Ids=["watch"]), events_c.delete_rule(Name=rule)))
    step("승인 링크", lambda: lam.delete_function_url_config(FunctionName=n["function"]))
    step("상시 감시 함수", lambda: lam.delete_function(FunctionName=n["function"]))
    step("기록 표", lambda: session.client("dynamodb").delete_table(TableName=n["table"]))
    step("로그 그룹", lambda: session.client("logs").delete_log_group(logGroupName=n["log_group"]))

    def untag() -> None:
        try:
            arn = _service(session, cluster, service)["serviceArn"]
        except WatchError:
            return
        session.client("ecs").untag_resource(resourceArn=arn, tagKeys=[WATCH_TAG])
    step("감시 표시 떼기", untag)
    failed = [r for r in results if not r["ok"]]
    return {"ok": not failed, "results": results,
            "message": "상시 감시를 지웠습니다." if not failed else f"{len(failed)}개를 지우지 못했습니다 — 목록을 확인하세요."}


def installed(session: Any, *, region: str, cluster: str, service: str) -> Optional[dict]:
    """정리 화면용 — 감시자 함수가 있으면 {name, arn, tagged}."""
    n = names(region, cluster, service)
    try:
        resp = session.client("lambda").get_function(FunctionName=n["function"])
    except Exception as exc:  # noqa: BLE001
        if _missing(exc):
            return None
        raise
    arn = resp["Configuration"]["FunctionArn"]
    #: GetFunction 은 lambda:ListTags 가 있어야 태그를 함께 준다 — 따로 묻는다(정리 화면이 'ReCoder 가 만든 것'만 지우게).
    try:
        tags = session.client("lambda").list_tags(Resource=arn).get("Tags") or {}
    except Exception:  # noqa: BLE001
        tags = resp.get("Tags") or {}
    return {"name": n["function"], "arn": arn, "tagged": tags.get(aws_infra.MANAGED_TAG) == "true"}
