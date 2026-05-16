"""
prometheus_adapter.py — PromQL 메트릭 쿼리 어댑터 (설계서 §Q4 ObservabilityAdapter).

설계서:
    PrometheusAdapter: 메트릭 쿼리, 에러율, 레이턴시, 메모리/CPU

본 어댑터는 다음 두 가지를 보장한다.
  1. requests 가 없거나 백엔드 미연결인 경우에도 import / 호출이 실패하지 않는다.
     → ObservabilityQueryResult.error 와 backend="fallback" 으로 표시한다.
  2. 모든 쿼리는 짧은 timeout 을 적용하고 raw 응답을 가공해 sample list 만 반환한다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
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


class PrometheusAdapter:
    """Prometheus HTTP API (`/api/v1/query`, `/api/v1/query_range`) 래퍼."""

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
    # Public — 단일 인스턴트 쿼리
    # ------------------------------------------------------------------

    def query(self, promql: str) -> ObservabilityQueryResult:
        started = datetime.now(timezone.utc)
        if not self.base_url or not _HAS_REQUESTS:
            return ObservabilityQueryResult(
                kind=ObservabilityQueryKind.METRIC,
                query=promql,
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                samples=[],
                error="prometheus backend unavailable",
                backend="fallback",
            )

        headers = {"Accept": "application/json"}
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"

        try:
            resp = requests.get(  # type: ignore[union-attr]
                f"{self.base_url}/api/v1/query",
                params={"query": promql},
                headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return ObservabilityQueryResult(
                kind=ObservabilityQueryKind.METRIC,
                query=promql,
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                samples=[],
                error=f"prometheus query failed: {exc}",
                backend="prometheus",
            )

        samples = _parse_prom_result(payload)
        return ObservabilityQueryResult(
            kind=ObservabilityQueryKind.METRIC,
            query=promql,
            started_at=started,
            ended_at=datetime.now(timezone.utc),
            samples=samples,
            error=None if samples else "no samples returned",
            backend="prometheus",
        )

    # ------------------------------------------------------------------
    # High-level helpers — Incident Correlation / RCA 가 부르는 함수들
    # ------------------------------------------------------------------

    def error_rate(self, service: str, window: str = "5m") -> ObservabilityQueryResult:
        promql = (
            f"sum(rate(http_requests_total{{service=\"{service}\",status=~\"5..\"}}[{window}]))"
            f" / sum(rate(http_requests_total{{service=\"{service}\"}}[{window}]))"
        )
        return self.query(promql)

    def latency_p95(self, service: str, window: str = "5m") -> ObservabilityQueryResult:
        promql = (
            f"histogram_quantile(0.95, sum(rate("
            f"http_request_duration_seconds_bucket{{service=\"{service}\"}}[{window}]"
            f")) by (le))"
        )
        return self.query(promql)

    def container_restart_count(
        self,
        container_name: str,
        window: str = "10m",
    ) -> ObservabilityQueryResult:
        promql = (
            f"increase(container_restarts_total{{container=\"{container_name}\"}}"
            f"[{window}])"
        )
        return self.query(promql)

    def memory_usage_bytes(self, container_name: str) -> ObservabilityQueryResult:
        promql = f"container_memory_usage_bytes{{container=\"{container_name}\"}}"
        return self.query(promql)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_prom_result(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Prometheus 응답에서 sample 리스트만 추출."""
    if not isinstance(payload, dict):
        return []
    if payload.get("status") != "success":
        return []
    data = payload.get("data") or {}
    rtype = data.get("resultType")
    result = data.get("result") or []
    samples: list[dict[str, Any]] = []
    if rtype == "vector":
        for item in result:
            ts, val = item.get("value", [None, None])
            samples.append({
                "labels": item.get("metric", {}),
                "timestamp": ts,
                "value": _to_float(val),
            })
    elif rtype == "matrix":
        for item in result:
            for ts, val in item.get("values", []):
                samples.append({
                    "labels": item.get("metric", {}),
                    "timestamp": ts,
                    "value": _to_float(val),
                })
    return samples


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["PrometheusAdapter"]
