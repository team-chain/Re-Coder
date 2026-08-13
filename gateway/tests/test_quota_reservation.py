"""
게이트웨이 쿼터 — Codex P1 회귀: 호출 **전** 원자적 예약.

사후 기록만으로는 (1) 한도 직전 학생이 큰 max_tokens 요청 하나로 한도를
뚫고, (2) 동시 요청 무리가 같은 카운터를 보고 전부 통과한다.
moto 로 DynamoDB 를 실제 흉내 내어 조건부 ADD 의 원자성을 검증한다.
"""
import os
import sys
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
             max_total=10_000, max_daily=1_000):
    table.put_item(Item={
        "pk": f"STUDENT#{sid}", "sk": "META",
        "used_total_tokens": used_total, "used_today_tokens": used_today,
        "max_total_tokens": max_total, "max_daily_tokens": max_daily,
        "today": common._today()})
    return {"pk": f"STUDENT#{sid}", "sk": "META",
            "used_total_tokens": used_total, "used_today_tokens": used_today,
            "max_total_tokens": max_total, "max_daily_tokens": max_daily,
            "today": common._today()}


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


def test_reconcile_returns_overreservation(table):
    """예약 600 → 실사용 200 이면 카운터가 200 으로 정산되고 풀 비용이 쌓인다."""
    item = _student(table)
    common.reserve_quota(item, 600)
    cost = common.reconcile_usage("s1", 600, 150, 50)
    assert _used(table) == (200, 200)
    pool = table.get_item(Key={"pk": "POOL", "sk": "META"})["Item"]
    assert int(pool["used_total_tokens"]) == 200
    assert cost > 0


def test_release_on_invoke_failure(table):
    """Bedrock 호출 실패 시 선점분이 전액 반환된다 — 실패한 호출이 한도를
    갉아먹으면 안 된다."""
    item = _student(table)
    common.reserve_quota(item, 600)
    common.release_reservation("s1", 600)
    assert _used(table) == (0, 0)
