"""
ReCoder Gateway — 공통 유틸 (토큰·쿼터·DynamoDB·Bedrock).

Phase 1: 운영자 AWS 계정의 Bedrock 을 학생이 키 없이 쓰도록 중계.
- 학생 토큰 인증 (토큰 원문은 저장 안 함, sha256 만 저장)
- per-student 쿼터 (총/일/분당) + 학생 풀 전체 $ 캡
- Bedrock Converse 호출 + 사용량 기록

모든 설정은 환경변수로 주입 (SAM 템플릿 참조).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

# ── 환경변수 ────────────────────────────────────────────────────────
TABLE_NAME      = os.environ.get("GW_TABLE", "RecoderGateway")
REGION          = os.environ.get("GW_REGION", os.environ.get("AWS_REGION", "us-east-1"))
ALLOWED_MODELS  = [m.strip() for m in os.environ.get(
    "GW_ALLOWED_MODELS", "anthropic.claude-3-haiku-20240307-v1:0").split(",") if m.strip()]
DEFAULT_MODEL   = ALLOWED_MODELS[0] if ALLOWED_MODELS else "anthropic.claude-3-haiku-20240307-v1:0"
# Haiku 단가 (USD per 1K tokens) — 콘솔에서 확인 후 조정
PRICE_IN_PER_1K  = float(os.environ.get("GW_PRICE_IN_PER_1K",  "0.00025"))
PRICE_OUT_PER_1K = float(os.environ.get("GW_PRICE_OUT_PER_1K", "0.00125"))
# 쿼터 기본값
DEF_MAX_TOTAL   = int(os.environ.get("GW_DEFAULT_MAX_TOTAL_TOKENS", "500000"))   # 1인 7일
DEF_MAX_DAILY   = int(os.environ.get("GW_DEFAULT_MAX_DAILY_TOKENS", "100000"))   # 1인 1일
DEF_RPM         = int(os.environ.get("GW_DEFAULT_RPM", "10"))                    # 분당
TOKEN_TTL_DAYS  = int(os.environ.get("GW_TOKEN_TTL_DAYS", "7"))                  # 토큰 만료
POOL_CAP_USD    = float(os.environ.get("GW_POOL_CAP_USD", "20"))                 # 학생 풀 전체 천장
POOL_SOFT_USD   = float(os.environ.get("GW_POOL_SOFT_USD", "18"))               # 게이트웨이 소프트 차단
ENROLL_MAX      = int(os.environ.get("GW_MAX_STUDENTS", "0"))                    # 0 = 무제한, >0 = 자가발급 정원
RESERVATION_TTL_SECONDS = max(
    60,
    int(os.environ.get("GW_RESERVATION_TTL_SECONDS", "120")),
)
RESERVATION_RECOVERY_LIMIT = max(
    1,
    int(os.environ.get("GW_RESERVATION_RECOVERY_LIMIT", "25")),
)

_ddb = None
def _table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    return _ddb

_bedrock = None
def _bedrock_client():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    return _bedrock


# ── 토큰 ────────────────────────────────────────────────────────────
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def generate_token(student_id: str) -> str:
    """rcdr_<student_id>_<secret>. 원문은 한 번만 반환, 저장은 hash 만."""
    return f"rcdr_{student_id}_{secrets.token_urlsafe(24)}"

def parse_student_id(token: str) -> str | None:
    if not token or not token.startswith("rcdr_"):
        return None
    parts = token.split("_", 2)
    return parts[1] if len(parts) == 3 else None


# ── 학생 레코드 ─────────────────────────────────────────────────────
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")

def put_student(student_id: str, token_hash: str, *, name: str = "",
                discord_user_id: str = "",
                max_total: int = DEF_MAX_TOTAL, max_daily: int = DEF_MAX_DAILY,
                rpm: int = DEF_RPM, ttl_days: int = TOKEN_TTL_DAYS) -> None:
    expires = int(time.time()) + ttl_days * 86400
    _table().put_item(Item={
        "pk": f"STUDENT#{student_id}", "sk": "META",
        "token_sha256": token_hash, "name": name,
        "discord_user_id": discord_user_id, "active": True,
        "max_total_tokens": max_total, "max_daily_tokens": max_daily, "rpm": rpm,
        "used_total_tokens": 0, "used_today_tokens": 0, "today": _today(),
        "ttl": expires, "created_at": datetime.now(timezone.utc).isoformat(),
    })
    # 1:1 바인딩: discord_user_id → student_id 역방향 조회 항목.
    # relay(Phase 2)가 디코 명령의 행위자를 본인 학생/connection 으로만 해석하도록 함.
    if discord_user_id:
        _table().put_item(Item={
            "pk": f"DISCORD#{discord_user_id}", "sk": "META",
            "student_id": student_id, "ttl": expires})

def get_student(student_id: str) -> dict | None:
    r = _table().get_item(Key={"pk": f"STUDENT#{student_id}", "sk": "META"})
    return r.get("Item")


def get_student_by_discord(discord_user_id: str) -> dict | None:
    """discord_user_id → 학생 META (1:1 바인딩 역방향 조회). relay 라우팅용."""
    r = _table().get_item(Key={"pk": f"DISCORD#{discord_user_id}", "sk": "META"})
    item = r.get("Item")
    if not item:
        return None
    return get_student(item["student_id"])


def _put_unique(item: dict) -> bool:
    """attribute_not_exists(pk) 조건부 삽입. 이미 있으면 False(충돌)."""
    try:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def _bump_enroll_count(delta: int) -> int:
    r = _table().update_item(
        Key={"pk": "POOL", "sk": "META"},
        UpdateExpression="ADD enrolled_count :d",
        ExpressionAttributeValues={":d": delta},
        ReturnValues="UPDATED_NEW")
    return int(r["Attributes"].get("enrolled_count", 0))


def enroll_student(*, name: str = "", discord_user_id: str = "",
                   max_total: int = DEF_MAX_TOTAL, max_daily: int = DEF_MAX_DAILY,
                   rpm: int = DEF_RPM, ttl_days: int = TOKEN_TTL_DAYS) -> tuple[str, str]:
    """
    자가발급: 난수 고유 student_id + 토큰을 생성. 절대 겹치지 않음.

    충돌 방지 2중:
      1) secret 192bit 난수 → 추측·충돌 불가
      2) student_id 조건부 삽입(attribute_not_exists) → 원자적 고유성, 충돌 시 재생성
    오남용 방지: 디스코드 1:1 중복 거부 + 정원(ENROLL_MAX) 제한.
    Returns (student_id, token). 실패 시 QuotaError.
    """
    if discord_user_id and get_student_by_discord(discord_user_id):
        raise QuotaError("already_enrolled", "이미 토큰이 발급된 디스코드 계정입니다.")

    if ENROLL_MAX:
        n = _bump_enroll_count(1)
        if n > ENROLL_MAX:
            _bump_enroll_count(-1)
            raise QuotaError("enroll_full", "트라이얼 정원이 가득 찼습니다.")

    expires = int(time.time()) + ttl_days * 86400
    for _ in range(6):
        sid = secrets.token_hex(8)          # 64bit, 언더스코어 없음 → 토큰 파싱 안전
        token = generate_token(sid)
        item = {
            "pk": f"STUDENT#{sid}", "sk": "META",
            "token_sha256": hash_token(token), "name": name,
            "discord_user_id": discord_user_id, "active": True,
            "max_total_tokens": max_total, "max_daily_tokens": max_daily, "rpm": rpm,
            "used_total_tokens": 0, "used_today_tokens": 0, "today": _today(),
            "ttl": expires, "created_at": datetime.now(timezone.utc).isoformat(),
            "self_enrolled": True,
        }
        if not _put_unique(item):
            continue                        # student_id 충돌 → 재생성
        if discord_user_id:
            # 디스코드 1:1 도 원자적으로(동시 enroll 경합 방지)
            if not _put_unique({"pk": f"DISCORD#{discord_user_id}", "sk": "META",
                                "student_id": sid, "ttl": expires}):
                set_student_active(sid, False)
                raise QuotaError("already_enrolled", "이미 토큰이 발급된 디스코드 계정입니다.")
        return sid, token

    if ENROLL_MAX:
        _bump_enroll_count(-1)
    raise QuotaError("enroll_failed", "토큰 생성에 실패했습니다. 다시 시도하세요.")

def set_student_active(student_id: str, active: bool) -> bool:
    try:
        _table().update_item(
            Key={"pk": f"STUDENT#{student_id}", "sk": "META"},
            UpdateExpression="SET active = :a",
            ExpressionAttributeValues={":a": active},
            ConditionExpression="attribute_exists(pk)")
        return True
    except ClientError:
        return False

def set_student_quota(student_id: str, *, max_total=None, max_daily=None, rpm=None) -> bool:
    sets, vals = [], {}
    if max_total is not None: sets.append("max_total_tokens = :mt"); vals[":mt"] = int(max_total)
    if max_daily is not None: sets.append("max_daily_tokens = :md"); vals[":md"] = int(max_daily)
    if rpm is not None:       sets.append("rpm = :rp");              vals[":rp"] = int(rpm)
    if not sets:
        return False
    try:
        _table().update_item(
            Key={"pk": f"STUDENT#{student_id}", "sk": "META"},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeValues=vals,
            ConditionExpression="attribute_exists(pk)")
        return True
    except ClientError:
        return False


class QuotaError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def authenticate(token: str) -> dict:
    """토큰 검증 → 학생 META 반환. 실패 시 QuotaError."""
    sid = parse_student_id(token)
    if not sid:
        raise QuotaError("invalid_token", "토큰 형식이 올바르지 않습니다.")
    item = get_student(sid)
    if not item:
        raise QuotaError("unknown_token", "등록되지 않은 토큰입니다.")
    if not item.get("active", False):
        raise QuotaError("revoked", "비활성화된 토큰입니다.")
    if item.get("token_sha256") != hash_token(token):
        raise QuotaError("invalid_token", "토큰이 일치하지 않습니다.")
    if int(item.get("ttl", 0)) and int(item["ttl"]) < int(time.time()):
        raise QuotaError("expired", "만료된 토큰입니다.")
    return item


def check_rate(student_id: str, rpm: int) -> None:
    """분당 요청 제한 (현재 분 윈도우 atomic counter)."""
    minute = int(time.time() // 60)
    try:
        r = _table().update_item(
            Key={"pk": f"RATE#{student_id}", "sk": str(minute)},
            UpdateExpression="ADD #c :one SET #ttl = :exp",
            ExpressionAttributeNames={"#c": "count", "#ttl": "ttl"},
            ExpressionAttributeValues={":one": 1, ":exp": (minute + 2) * 60},
            ReturnValues="UPDATED_NEW")
        if int(r["Attributes"]["count"]) > rpm:
            raise QuotaError("rate_limited", f"분당 요청 한도({rpm})를 초과했습니다.")
    except ClientError:
        pass  # rate 카운터 장애가 서비스를 막지 않도록


def check_quota_before(item: dict) -> None:
    """호출 전 누적 사용량을 빠르게 확인하되 카운터는 변경하지 않는다.

    날짜 변경과 당일 예산 증가는 ``reserve_quota`` 의 단일 조건부 연산에서
    처리한다. 여기서 별도로 0으로 초기화하면, 같은 stale 레코드를 읽은 동시
    요청이 다른 요청의 예약을 지울 수 있다.
    """
    if int(item.get("used_total_tokens", 0)) >= int(item.get("max_total_tokens", DEF_MAX_TOTAL)):
        raise QuotaError("total_exceeded", "총 토큰 한도를 모두 사용했습니다.")
    if (item.get("today") == _today()
            and int(item.get("used_today_tokens", 0))
            >= int(item.get("max_daily_tokens", DEF_MAX_DAILY))):
        raise QuotaError("daily_exceeded", "오늘의 토큰 한도를 모두 사용했습니다.")
    # 풀 전체 소프트 캡
    pool = get_pool()
    if float(pool.get("used_cost_usd", 0)) >= POOL_SOFT_USD:
        raise QuotaError("pool_exceeded", "학생 풀 전체 한도에 도달했습니다.")


def get_pool() -> dict:
    r = _table().get_item(Key={"pk": "POOL", "sk": "META"})
    return r.get("Item", {"used_total_tokens": 0, "used_cost_usd": 0, "cap_usd": POOL_CAP_USD})


# 호출당 출력 토큰 상한 — 호출자가 보내는 max_tokens 는 비신뢰 입력이다.
MAX_TOKENS_CEILING = max(
    1,
    int(os.environ.get("GW_MAX_TOKENS_CEILING", "4096")),
)


def estimate_request_tokens(messages, system: str, max_output_tokens: int) -> int:
    """예약할 토큰 예산 = 입력 **상한** + 출력 상한.

    예약은 반드시 실제 사용량의 **상한**이어야 한다. `chars // 4`(영문 근사)는
    CJK·이모지·랜덤 식별자·인코딩 데이터에서 실제 Bedrock 입력 토큰을 크게
    과소평가한다 — 그러면 한도 직전 학생이 작은 예산만 예약하고 초과분은
    사후 정산으로만 반영되어, 예약이 보장하려던 캡이 다시 뚫린다.

    그래서 Converse 에 전달되는 메시지 구조 전체를 compact JSON 으로 직렬화한
    **UTF-8 바이트 수**를 상한으로 쓴다. role/content/text 키와 배열·객체 구분자가
    메시지와 콘텐츠 블록마다 반복되므로 짧은 턴이 많아도 framing 비용이 함께
    증가한다. 바이너리 블록은 base64 로 확장해 계산한다. 고정 64는 모델별 내부
    프롬프트 여유분이다. 과대분은 reconcile 이 되돌린다.
    """
    payload = {"messages": messages or []}
    if system:
        payload["system"] = [{"text": system}]

    def _json_default(value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return base64.b64encode(bytes(value)).decode("ascii")
        return str(value)

    framed_bytes = len(json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8"))
    return framed_bytes + 64 + int(max_output_tokens)


def _reservation_cost(budget_tokens: int):
    """Conservative USD upper bound for a token reservation.

    The input/output split is unknown before the call, so charge every reserved
    token at the more expensive configured rate. Reconciliation returns the
    over-reservation after Bedrock reports actual usage.
    """
    return _dec((int(budget_tokens) / 1000.0) * max(
        PRICE_IN_PER_1K,
        PRICE_OUT_PER_1K,
    ))


@dataclass(frozen=True)
class QuotaReservation:
    reservation_id: str
    reservation_key: str
    student_id: str
    budget_tokens: int
    reservation_day: str
    reserved_cost_usd: Decimal
    expires_at: int


def _now_epoch() -> int:
    return int(time.time())


def _serialize_values(values: dict) -> dict:
    # The client attached to a DynamoDB resource shares the resource's native
    # Python-value transformer, including for transact_write_items.
    return values


def _transaction_cancelled(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in {
        "ConditionalCheckFailedException",
        "TransactionCanceledException",
    }


def _reservation_from_item(item: dict) -> QuotaReservation:
    return QuotaReservation(
        reservation_id=str(item["reservation_id"]),
        reservation_key=str(item["sk"]),
        student_id=str(item["student_id"]),
        budget_tokens=int(item["budget_tokens"]),
        reservation_day=str(item["reservation_day"]),
        reserved_cost_usd=item["reserved_cost_usd"],
        expires_at=int(item["expires_at"]),
    )


def _reservation_transaction(
    student_id: str,
    student_update: dict,
    reservation: QuotaReservation,
) -> None:
    table = _table()
    soft_cap = _dec(POOL_SOFT_USD)
    reserved_cost = reservation.reserved_cost_usd
    table.meta.client.transact_write_items(TransactItems=[
        {
            "Update": {
                "TableName": table.name,
                "Key": _serialize_values({"pk": "POOL", "sk": "META"}),
                "UpdateExpression": "ADD used_cost_usd :c",
                "ConditionExpression": (
                    "attribute_not_exists(used_cost_usd) "
                    "OR used_cost_usd <= :remaining"
                ),
                "ExpressionAttributeValues": _serialize_values({
                    ":c": reserved_cost,
                    ":remaining": soft_cap - reserved_cost,
                }),
            }
        },
        {
            "Update": {
                "TableName": table.name,
                "Key": _serialize_values({
                    "pk": f"STUDENT#{student_id}",
                    "sk": "META",
                }),
                "UpdateExpression": student_update["UpdateExpression"],
                "ConditionExpression": student_update["ConditionExpression"],
                "ExpressionAttributeValues": _serialize_values(
                    student_update["ExpressionAttributeValues"]
                ),
            }
        },
        {
            "Put": {
                "TableName": table.name,
                "Item": _serialize_values({
                    "pk": "RESERVATION",
                    "sk": reservation.reservation_key,
                    "reservation_id": reservation.reservation_id,
                    "student_id": reservation.student_id,
                    "budget_tokens": reservation.budget_tokens,
                    "reservation_day": reservation.reservation_day,
                    "reserved_cost_usd": reservation.reserved_cost_usd,
                    "expires_at": reservation.expires_at,
                    "created_at": _now_epoch(),
                }),
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        },
    ])


def _settle_reservation(
    reservation: QuotaReservation,
    *,
    student_delta: int,
    actual_tokens: int,
    pool_cost_delta,
) -> bool:
    """Atomically adjust counters and consume one reservation record."""
    table = _table()
    pool_values = {":c": pool_cost_delta}
    pool_expression = "ADD used_cost_usd :c"
    if int(actual_tokens) != 0:
        pool_expression += ", used_total_tokens :t"
        pool_values[":t"] = int(actual_tokens)

    base_items = [{
        "Update": {
            "TableName": table.name,
            "Key": _serialize_values({"pk": "POOL", "sk": "META"}),
            "UpdateExpression": pool_expression,
            "ExpressionAttributeValues": _serialize_values(pool_values),
        }
    }]
    delete_item = {
        "Delete": {
            "TableName": table.name,
            "Key": _serialize_values({
                "pk": "RESERVATION",
                "sk": reservation.reservation_key,
            }),
            "ConditionExpression": "reservation_id = :reservation_id",
            "ExpressionAttributeValues": _serialize_values({
                ":reservation_id": reservation.reservation_id,
            }),
        }
    }

    if int(student_delta) == 0:
        attempts = [base_items + [delete_item]]
    else:
        student_key = _serialize_values({
            "pk": f"STUDENT#{reservation.student_id}",
            "sk": "META",
        })
        same_day = {
            "Update": {
                "TableName": table.name,
                "Key": student_key,
                "UpdateExpression": (
                    "ADD used_total_tokens :d, used_today_tokens :d"
                ),
                "ConditionExpression": "today = :reservation_day",
                "ExpressionAttributeValues": _serialize_values({
                    ":d": int(student_delta),
                    ":reservation_day": reservation.reservation_day,
                }),
            }
        }
        other_day = {
            "Update": {
                "TableName": table.name,
                "Key": student_key,
                "UpdateExpression": "ADD used_total_tokens :d",
                "ConditionExpression": (
                    "attribute_not_exists(today) OR today <> :reservation_day"
                ),
                "ExpressionAttributeValues": _serialize_values({
                    ":d": int(student_delta),
                    ":reservation_day": reservation.reservation_day,
                }),
            }
        }
        attempts = [
            base_items + [same_day, delete_item],
            base_items + [other_day, delete_item],
        ]

    for transaction_items in attempts:
        try:
            table.meta.client.transact_write_items(
                TransactItems=transaction_items,
            )
            return True
        except ClientError as exc:
            if not _transaction_cancelled(exc):
                raise
    return False


def recover_expired_reservations(*, limit: int | None = None) -> int:
    """Release bounded, expired reservations left by terminated Lambdas."""
    cutoff = f"{_now_epoch():010d}#\uffff"
    response = _table().query(
        KeyConditionExpression="pk = :pk AND sk <= :cutoff",
        ExpressionAttributeValues={
            ":pk": "RESERVATION",
            ":cutoff": cutoff,
        },
        ConsistentRead=True,
        Limit=int(limit or RESERVATION_RECOVERY_LIMIT),
    )
    recovered = 0
    for item in response.get("Items", []):
        reservation = _reservation_from_item(item)
        if _settle_reservation(
            reservation,
            student_delta=-reservation.budget_tokens,
            actual_tokens=0,
            pool_cost_delta=-reservation.reserved_cost_usd,
        ):
            recovered += 1
    return recovered


def reserve_quota(item: dict, budget_tokens: int) -> QuotaReservation:
    """Atomically reserve student/pool quota and create an expiring record."""
    budget = int(budget_tokens)
    if budget <= 0:
        raise QuotaError("invalid_budget", "예약 토큰은 1 이상이어야 합니다.")

    # A later invocation repairs reservations orphaned by timeout/crash before
    # it is allowed to consume more paid quota.
    recover_expired_reservations()

    sid = item["pk"].split("#", 1)[1]
    max_total = int(item.get("max_total_tokens", DEF_MAX_TOTAL))
    max_daily = int(item.get("max_daily_tokens", DEF_MAX_DAILY))
    today = _today()
    reserved_cost = _reservation_cost(budget)
    if reserved_cost > _dec(POOL_SOFT_USD):
        raise QuotaError("pool_exceeded", "요청 예산이 공유 비용 한도를 초과합니다.")
    if budget > max_daily:
        raise QuotaError("daily_exceeded", "요청 예산이 일일 토큰 한도를 초과합니다.")
    if budget > max_total:
        raise QuotaError("total_exceeded", "요청 예산이 전체 토큰 한도를 초과합니다.")

    expires_at = _now_epoch() + RESERVATION_TTL_SECONDS
    reservation_id = secrets.token_hex(16)
    reservation = QuotaReservation(
        reservation_id=reservation_id,
        reservation_key=f"{expires_at:010d}#{reservation_id}",
        student_id=sid,
        budget_tokens=budget,
        reservation_day=today,
        reserved_cost_usd=reserved_cost,
        expires_at=expires_at,
    )
    same_day = {
        "UpdateExpression": "ADD used_total_tokens :b, used_today_tokens :b",
        "ConditionExpression": (
            "(attribute_not_exists(used_total_tokens) OR used_total_tokens <= :tm) "
            "AND today = :today "
            "AND (attribute_not_exists(used_today_tokens) OR used_today_tokens <= :dm)"
        ),
        "ExpressionAttributeValues": {
            ":b": budget,
            ":tm": max_total - budget,
            ":dm": max_daily - budget,
            ":today": today,
        },
    }
    rollover = {
        "UpdateExpression": (
            "SET used_today_tokens = :b, today = :today "
            "ADD used_total_tokens :b"
        ),
        "ConditionExpression": (
            "(attribute_not_exists(used_total_tokens) OR used_total_tokens <= :tm) "
            "AND (attribute_not_exists(today) OR today <> :today) "
            "AND :b <= :daily"
        ),
        "ExpressionAttributeValues": {
            ":b": budget,
            ":tm": max_total - budget,
            ":daily": max_daily,
            ":today": today,
        },
    }
    attempts = (
        (same_day, rollover)
        if item.get("today") == today
        else (rollover, same_day)
    )
    for update in attempts:
        try:
            _reservation_transaction(sid, update, reservation)
            return reservation
        except ClientError as exc:
            if not _transaction_cancelled(exc):
                raise

    pool = _table().get_item(
        Key={"pk": "POOL", "sk": "META"},
        ConsistentRead=True,
    ).get("Item", {})
    if pool.get("used_cost_usd", _dec(0)) > _dec(POOL_SOFT_USD) - reserved_cost:
        raise QuotaError("pool_exceeded", "학생 전체 공유 한도에 도달했습니다.")

    current = _table().get_item(
        Key={"pk": f"STUDENT#{sid}", "sk": "META"},
        ConsistentRead=True,
    ).get("Item", {})
    if int(current.get("used_total_tokens", 0)) > max_total - budget:
        raise QuotaError("total_exceeded", "전체 토큰 한도에 도달했습니다.")
    raise QuotaError("daily_exceeded", "일일 토큰 한도에 도달했습니다.")


def release_reservation(reservation: QuotaReservation) -> None:
    """Release a failed call exactly once."""
    if not _settle_reservation(
        reservation,
        student_delta=-reservation.budget_tokens,
        actual_tokens=0,
        pool_cost_delta=-reservation.reserved_cost_usd,
    ):
        raise QuotaError("reservation_expired", "예약이 이미 만료되었거나 정산되었습니다.")


def reconcile_usage(
    reservation: QuotaReservation,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Consume one reservation and atomically reconcile actual usage."""
    cost = (
        (input_tokens / 1000.0) * PRICE_IN_PER_1K
        + (output_tokens / 1000.0) * PRICE_OUT_PER_1K
    )
    actual = int(input_tokens) + int(output_tokens)
    if not _settle_reservation(
        reservation,
        student_delta=actual - reservation.budget_tokens,
        actual_tokens=actual,
        pool_cost_delta=_dec(cost) - reservation.reserved_cost_usd,
    ):
        raise QuotaError("reservation_expired", "예약이 이미 만료되었거나 정산되었습니다.")
    return cost


def record_usage(student_id: str, input_tokens: int, output_tokens: int) -> float:
    """사용량을 학생·풀에 atomic 반영. 이번 호출 비용(USD) 반환."""
    cost = (input_tokens / 1000.0) * PRICE_IN_PER_1K + (output_tokens / 1000.0) * PRICE_OUT_PER_1K
    total = int(input_tokens) + int(output_tokens)
    _table().update_item(
        Key={"pk": f"STUDENT#{student_id}", "sk": "META"},
        UpdateExpression="ADD used_total_tokens :t, used_today_tokens :t",
        ExpressionAttributeValues={":t": total})
    _table().update_item(
        Key={"pk": "POOL", "sk": "META"},
        UpdateExpression="ADD used_total_tokens :t, used_cost_usd :c",
        ExpressionAttributeValues={":t": total, ":c": _dec(cost)})
    return cost


def _dec(x: float):
    return Decimal(str(round(x, 6)))


# ── Bedrock ─────────────────────────────────────────────────────────
def invoke_bedrock(messages: list, *, model: str | None = None,
                   system: str = "", max_tokens: int = 2048,
                   output_schema: dict | None = None) -> dict:
    """Converse 호출. 모델은 서버 allowlist 로 강제."""
    use_model = model if model in ALLOWED_MODELS else DEFAULT_MODEL
    kwargs = {
        "modelId": use_model,
        "messages": messages,
        "inferenceConfig": {"maxTokens": int(max_tokens), "temperature": 0.0},
    }
    if system:
        kwargs["system"] = [{"text": system}]
    resp = _bedrock_client().converse(**kwargs)
    out = resp.get("output", {}).get("message", {}).get("content", [])
    text = "".join(c.get("text", "") for c in out)
    usage = resp.get("usage", {})
    input_tok = int(usage.get("inputTokens", 0))
    output_tok = int(usage.get("outputTokens", 0))
    parsed = None
    if output_schema is not None:
        parsed = _extract_json(text)
    return {
        "text": text, "parsed": parsed, "model_used": use_model,
        "input_tokens": input_tok, "output_tokens": output_tok,
    }


def _extract_json(text: str):
    import re
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


# ── HTTP 응답 헬퍼 ─────────────────────────────────────────────────
def resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }
