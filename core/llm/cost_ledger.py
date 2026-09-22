"""LLM 비용 원장 — 모든 호출 기록이 모이는 **한 곳**.

보드 이슈 「LLM 라우터가 두 벌 — 비용 집계와 폴백이 갈라짐」의 일부.
예전에는 llm/router.py 가 response.metadata 에, provider_router 가 자기
인스턴스 리스트에 따로 적었다 — 어느 쪽을 읽어도 전체 비용이 아니었다.
이제 두 진입점 모두 여기로 적는다. 읽는 쪽은 total_cost_usd() 하나면 된다.
"""
from __future__ import annotations

import threading
import time
from typing import Any

_LOCK = threading.Lock()
_RECORDS: list[dict[str, Any]] = []

#: 무한히 자라지 않게 — 코어는 장수 프로세스다.
_MAX_RECORDS = 5000


def record(entry: dict[str, Any]) -> dict[str, Any]:
    """호출 기록 하나를 원장에 적는다. ts 가 없으면 채운다."""
    entry = dict(entry)
    entry.setdefault("ts", time.time())
    with _LOCK:
        _RECORDS.append(entry)
        if len(_RECORDS) > _MAX_RECORDS:
            del _RECORDS[: len(_RECORDS) - _MAX_RECORDS]
    return entry


def all_records() -> list[dict[str, Any]]:
    with _LOCK:
        return list(_RECORDS)


def total_cost_usd() -> float:
    with _LOCK:
        return sum(float(r.get("estimated_cost_usd") or 0.0) for r in _RECORDS)


def reset() -> None:
    """테스트 전용."""
    with _LOCK:
        _RECORDS.clear()
