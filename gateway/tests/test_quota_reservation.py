"""
게이트웨이 쿼터 — Codex P1 회귀: 호출 **전** 원자적 예약.

사후 기록만으로는 (1) 한도 직전 학생이 큰 max_tokens 요청 하나로 한도를
뚫고, (2) 동시 요청 무리가 같은 카운터를 보고 전부 통과한다.
moto 로 DynamoDB 를 실제 흉내 내어 조건부 ADD 의 원자성을 검증한다.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

os.environ.setdefault("GW_TABLE", "RecoderGatewayTest")
os.environ.setdefault("GW_REGION", "us-east-1")

import common  # noqa: E402
import invoke  # noqa: E402


@pytest.fixture()
def table(monkeypatch):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName="RecoderGatewayTest",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        monkeypatch.setattr(common, "_ddb", t)
        yield t


def _student(table, sid="s1", used_total=0, used_today=0,
             max_total=10_000, max_daily=1_000, today=None):
    record_day = today or common._today()
    table.put_item(Item={
        "pk": f"STUDENT#{sid}", "sk": "META",
        "used_total_tokens": used_total, "used_today_tokens": used_today,
        "max_total_tokens": max_total, "max_daily_tokens": max_daily,
        "today": record_day})
    return {"pk": f"STUDENT#{sid}", "sk": "META",
            "used_total_tokens": used_total, "used_today_tokens": used_today,
            "max_total_tokens": max_total, "max_daily_tokens": max_daily,
            "today": record_day}


def _used(table, sid="s1"):
    it = table.get_item(Key={"pk": f"STUDENT#{sid}", "sk": "META"})["Item"]
    return int(it["used_total_tokens"]), int(it["used_today_tokens"])


def test_single_request_cannot_blow_past_remaining_allowance(table):
    """[Codex P1 본판] 잔여 100 토큰인 학생의 500 토큰 예산 요청은
    **호출 전에** 거부되고, 카운터는 오르지 않는다."""
    item = _student(table, used_today=900, max_daily=1_000)
    with pytest.raises(common.QuotaError):
        common.reserve_quota(item, 500)
    assert _used(table) == (0, 900), "거부된 예약이 카운터를 남겼다"


def test_concurrent_style_reservations_serialize_on_the_counter(table):
    """[Codex P1 본판] 둘 다 '사전 확인'은 통과할 두 요청 — 예약은 첫
    번째가 카운터를 올려 두 번째를 거부한다. 조건 검사와 ADD 가 한
    연산이므로 읽기-쓰기 사이 틈이 없다."""
    item = _student(table, used_today=0, max_daily=1_000)
    common.check_quota_before(dict(item))   # 옛 검사로는 둘 다 통과할 상태
    common.reserve_quota(item, 600)
    with pytest.raises(common.QuotaError):
        common.reserve_quota(item, 600)     # 잔여 400 < 600
    assert _used(table)[1] == 600


def test_rollover_reset_and_reservation_are_atomic(table):
    """두 stale 요청 중 뒤 요청이 앞 요청의 새 날짜 예약을 0으로 지우지 않는다."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    stale = _student(
        table,
        used_today=900,
        max_daily=1_000,
        today=yesterday,
    )
    first_snapshot = dict(stale)
    second_snapshot = dict(stale)

    # 빠른 검사는 날짜 전환 시 카운터를 변경하지 않는다.
    common.check_quota_before(first_snapshot)
    assert _used(table) == (0, 900)

    common.reserve_quota(first_snapshot, 600)
    with pytest.raises(common.QuotaError):
        common.reserve_quota(second_snapshot, 600)

    item = table.get_item(Key={"pk": "STUDENT#s1", "sk": "META"})["Item"]
    assert item["today"] == common._today()
    assert int(item["used_today_tokens"]) == 600
    assert int(item["used_total_tokens"]) == 600


def test_rollover_rejects_budget_larger_than_daily_cap(table):
    """새 UTC 날짜의 첫 예약도 일일 한도보다 큰 예산을 허용하지 않는다."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    stale = _student(
        table,
        used_today=900,
        max_daily=1_000,
        today=yesterday,
    )

    with pytest.raises(common.QuotaError) as exc:
        common.reserve_quota(stale, 1_200)

    assert exc.value.code == "daily_exceeded"
    item = table.get_item(Key={"pk": "STUDENT#s1", "sk": "META"})["Item"]
    assert item["today"] == yesterday
    assert int(item["used_today_tokens"]) == 900
    assert int(item["used_total_tokens"]) == 0
    pool = table.get_item(Key={"pk": "POOL", "sk": "META"}).get("Item", {})
    assert float(pool.get("used_cost_usd", 0)) == pytest.approx(0.0)


def test_reconcile_returns_overreservation(table):
    """예약 600 → 실사용 200 이면 카운터가 200 으로 정산되고 풀 비용이 쌓인다."""
    item = _student(table)
    reservation = common.reserve_quota(item, 600)
    cost = common.reconcile_usage(reservation, 150, 50)
    assert _used(table) == (200, 200)
    pool = table.get_item(Key={"pk": "POOL", "sk": "META"})["Item"]
    assert int(pool["used_total_tokens"]) == 200
    assert cost > 0


def test_shared_pool_is_reserved_before_paid_call(table, monkeypatch):
    """동시 요청은 유료 호출 전에 같은 풀 비용 캡을 원자적으로 선점한다."""
    monkeypatch.setattr(common, "POOL_SOFT_USD", 0.001)
    first = _student(table, sid="s1", max_daily=10_000)
    second = _student(table, sid="s2", max_daily=10_000)

    common.reserve_quota(first, 600)  # worst-case $0.00075
    pool = table.get_item(Key={"pk": "POOL", "sk": "META"})["Item"]
    assert float(pool["used_cost_usd"]) == pytest.approx(0.00075)

    with pytest.raises(common.QuotaError) as exc:
        common.reserve_quota(second, 600)
    assert exc.value.code == "pool_exceeded"
    # 거부된 두 번째 요청은 학생 카운터를 건드리지 않는다.
    assert _used(table, "s2") == (0, 0)


def test_pool_reservation_is_reconciled_to_actual_cost(table, monkeypatch):
    monkeypatch.setattr(common, "POOL_SOFT_USD", 1.0)
    item = _student(table)
    reservation = common.reserve_quota(item, 600)
    actual_cost = common.reconcile_usage(reservation, 150, 50)
    pool = table.get_item(Key={"pk": "POOL", "sk": "META"})["Item"]
    assert float(pool["used_cost_usd"]) == pytest.approx(actual_cost)


def test_release_on_invoke_failure(table):
    """Bedrock 호출 실패 시 선점분이 전액 반환된다 — 실패한 호출이 한도를
    갉아먹으면 안 된다."""
    item = _student(table)
    reservation = common.reserve_quota(item, 600)
    common.release_reservation(reservation)
    assert _used(table) == (0, 0)
    pool = table.get_item(Key={"pk": "POOL", "sk": "META"})["Item"]
    assert float(pool["used_cost_usd"]) == pytest.approx(0.0)


def test_expired_orphan_reservation_is_recovered(table, monkeypatch):
    """A later invocation can release quota left by a terminated Lambda."""
    clock = {"now": 1_700_000_000}
    monkeypatch.setattr(common, "_now_epoch", lambda: clock["now"])
    item = _student(table)

    reservation = common.reserve_quota(item, 600)
    stored = table.get_item(Key={
        "pk": "RESERVATION",
        "sk": reservation.reservation_key,
    }).get("Item")
    assert stored is not None
    assert _used(table) == (600, 600)

    clock["now"] = reservation.expires_at + 1
    assert common.recover_expired_reservations() == 1
    assert _used(table) == (0, 0)
    pool = table.get_item(Key={"pk": "POOL", "sk": "META"})["Item"]
    assert float(pool["used_cost_usd"]) == pytest.approx(0.0)
    assert table.get_item(Key={
        "pk": "RESERVATION",
        "sk": reservation.reservation_key,
    }).get("Item") is None
    assert common.recover_expired_reservations() == 0


def test_cross_midnight_release_does_not_decrement_new_day(table):
    """실패 반환은 예약 뒤 시작된 새 UTC 날짜의 카운터를 차감하지 않는다."""
    item = _student(table)
    reservation = common.reserve_quota(item, 600)
    next_day = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y%m%d")
    table.update_item(
        Key={"pk": "STUDENT#s1", "sk": "META"},
        UpdateExpression="SET today = :day, used_today_tokens = :used",
        ExpressionAttributeValues={":day": next_day, ":used": 125},
    )

    common.release_reservation(reservation)

    assert _used(table) == (0, 125)


def test_cross_midnight_reconcile_does_not_decrement_new_day(table):
    """과대 예약 정산은 새 날짜 사용량을 음수 방향으로 오염시키지 않는다."""
    item = _student(table)
    reservation = common.reserve_quota(item, 600)
    next_day = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y%m%d")
    table.update_item(
        Key={"pk": "STUDENT#s1", "sk": "META"},
        UpdateExpression="SET today = :day, used_today_tokens = :used",
        ExpressionAttributeValues={":day": next_day, ":used": 125},
    )

    common.reconcile_usage(reservation, 150, 50)

    assert _used(table) == (200, 125)


@pytest.mark.parametrize("bad_value", [-1, 0, "not-an-int"])
def test_invoke_rejects_invalid_max_tokens_before_reservation(monkeypatch, bad_value):
    calls = []
    monkeypatch.setattr(common, "authenticate", lambda token: {
        "pk": "STUDENT#s1", "rpm": 10,
    })
    monkeypatch.setattr(common, "check_rate", lambda *args: None)
    monkeypatch.setattr(common, "check_quota_before", lambda *args: None)
    monkeypatch.setattr(common, "reserve_quota", lambda *args: calls.append(args))

    response = invoke.handler({
        "headers": {"Authorization": "Bearer token"},
        "body": __import__("json").dumps({
            "prompt": "hello",
            "max_tokens": bad_value,
        }),
    }, None)
    assert response["statusCode"] == 400
    assert calls == []


def test_reservation_is_upper_bound_for_cjk(table):
    """[Codex P1 회귀] CJK/이모지는 chars//4 로 과소예약되면 캡을 뚫는다 —
    예약이 실제 토큰의 상한(UTF-8 바이트 이상)이어야 한다."""
    text = "가" * 300                       # 900 bytes, 실제 토큰 대략 300+
    msgs = [{"content": [{"text": text}]}]
    budget = common.estimate_request_tokens(msgs, "", max_output_tokens=0)
    assert budget >= len(text.encode("utf-8")), "예약이 바이트 상한 미만 — 과소예약"

    # 한도 직전 학생: 잔여 200. CJK 요청 예약이 이를 초과하므로 거부돼야 한다.
    item = _student(table, used_today=800, max_daily=1_000)
    with pytest.raises(common.QuotaError):
        common.reserve_quota(item, budget)


def test_reservation_scales_with_message_and_content_framing(table):
    """Many tiny turns reserve their repeated Converse framing before the call."""
    messages = [
        {"role": "user" if index % 2 == 0 else "assistant",
         "content": [{"text": "x"}, {"text": "y"}]}
        for index in range(100)
    ]
    budget = common.estimate_request_tokens(
        messages,
        "system",
        max_output_tokens=0,
    )
    wire_payload = {
        "messages": messages,
        "system": [{"text": "system"}],
    }
    framed_bytes = len(json.dumps(
        wire_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8"))

    assert budget == framed_bytes + 64
    raw_text_only = 200 + len("system") + 64
    assert budget > raw_text_only

    # The old fixed-overhead estimator would have admitted this request. The
    # framing-aware budget must reject it before a paid call can cross the cap.
    item = _student(table, max_daily=raw_text_only)
    with pytest.raises(common.QuotaError) as exc:
        common.reserve_quota(item, budget)
    assert exc.value.code == "daily_exceeded"


def test_english_still_reserves_and_reconciles(table):
    """[음성 대조] 영문은 과대 예약되지만 reconcile 이 되돌린다 — 캡은 지키되
    낭비는 정산으로 회수."""
    item = _student(table, max_daily=100_000)
    budget = common.estimate_request_tokens(
        [{"content": [{"text": "hello world " * 10}]}], "", max_output_tokens=50)
    reservation = common.reserve_quota(item, budget)
    common.reconcile_usage(reservation, 20, 30)   # 실제 50
    used_total, used_today = _used(table)
    assert used_today == 50, f"정산 후 실사용만 남아야: {used_today}"
