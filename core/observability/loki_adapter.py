"""
loki_adapter.py — LogQL 로그 쿼리 어댑터 (설계서 §Q4 ObservabilityAdapter).

설계서:
    LokiAdapter: 로그 쿼리, 컨테이너 로그 발췌, 에러 키워드 검색

원칙:
  - requests 가 없거나 백엔드 미연결인 경우에도 import / 호출이 실패하지 않는다.
  - 모든 결과는 ObservabilityQueryResult 의 samples 리스트로 정규화한다.
    각 sample 은 { timestamp, line, labels } 형태.
  - 시크릿이 포함될 수 있는 logs 는 호출 측에서 masking 후 사용한다
    (본 어댑터는 raw 응답만 가공한다 — 마스킹 책임은 Context Gate).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:
    import requests  # type: ignore
    _HAS_REQUESTS = True
except Exception:  # pragma: no cover
    requests = None  # type: ignore
    _HAS_REQUESTS = False

log = logging.getLogger(__name__)

try:
    from schemas import ObservabilityQueryKind, ObservabilityQueryResult
except ImportError:
    from core.schemas import ObservabilityQueryKind, ObservabilityQueryResult  # type: ignore


class LokiAdapter:
    """Loki HTTP API (`/loki/api/v1/query_range`) 래퍼."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_sec: float = 5.0,
        bearer_token: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout_sec
        self.bearer = bearer_token

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def query_range(
        self,
        logql: str,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 200,
    ) -> ObservabilityQueryResult:
        started = datetime.now(timezone.utc)
        end_ts = end or started
        start_ts = start or (end_ts - timedelta(minutes=15))

        if not self.base_url or not _HAS_REQUESTS:
            return ObservabilityQueryResult(
                kind=ObservabilityQueryKind.LOG,
                query=logql,
                started_at=start_ts,
                ended_at=end_ts,
                samples=[],
                error="loki backend unavailable",
                backend="fallback",
            )

        params = {
            "query": logql,
            "start": _to_ns(start_ts),
            "end": _to_ns(end_ts),
            "limit": str(limit),
            "direction": "forward",
        }
        headers = {"Accept": "application/json"}
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"

        try:
            resp = requests.get(  # type: ignore[union-attr]
                f"{self.base_url}/loki/api/v1/query_range",
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return ObservabilityQueryResult(
                kind=ObservabilityQueryKind.LOG,
                query=logql,
                started_at=start_ts,
                ended_at=end_ts,
                samples=[],
                error=f"loki query failed: {exc}",
                backend="loki",
            )

        samples = _parse_loki_result(payload)
        return ObservabilityQueryResult(
            kind=ObservabilityQueryKind.LOG,
            query=logql,
            started_at=start_ts,
            ended_at=end_ts,
            samples=samples,
            error=None if samples else "no log lines returned",
            backend="loki",
        )

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    def container_errors(
        self,
        container_name: str,
        *,
        minutes: int = 15,
        keyword: str = "error",
        limit: int = 100,
    ) -> ObservabilityQueryResult:
        # LogQL: 컨테이너 라벨로 필터 + 키워드 case-insensitive
        logql = (
            f'{{container="{container_name}"}} |~ "(?i){keyword}"'
        )
        end_ts = datetime.now(timezone.utc)
        return self.query_range(
            logql,
            start=end_ts - timedelta(minutes=minutes),
            end=end_ts,
            limit=limit,
        )

    def excerpt_around(
        self,
        container_name: str,
        center: datetime,
        *,
        before_seconds: int = 60,
        after_seconds: int = 60,
        limit: int = 60,
    ) -> ObservabilityQueryResult:
        logql = f'{{container="{container_name}"}}'
        return self.query_range(
            logql,
            start=center - timedelta(seconds=before_seconds),
            end=center + timedelta(seconds=after_seconds),
            limit=limit,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_ns(dt: datetime) -> str:
    """Loki 가 받는 epoch-nanosecond 문자열."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1_000_000_000))


def _parse_loki_result(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    if payload.get("status") != "success":
        return []
    data = payload.get("data") or {}
    rtype = data.get("resultType")
    result = data.get("result") or []
    samples: list[dict[str, Any]] = []
    if rtype == "streams":
        for stream in result:
            labels = stream.get("stream", {})
            for ns_ts, line in stream.get("values", []):
                samples.append({
                    "labels": labels,
                    "timestamp_ns": ns_ts,
                    "line": line,
                })
    elif rtype == "matrix":  # 메트릭 형식으로 떨어지는 LogQL agg 도 잡는다
        for item in result:
            for ts, val in item.get("values", []):
                samples.append({
                    "labels": item.get("metric", {}),
                    "timestamp": ts,
                    "value": val,
                })
    return samples


__all__ = ["LokiAdapter"]
