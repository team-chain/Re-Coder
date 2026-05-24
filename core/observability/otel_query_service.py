"""
otel_query_service.py — Incident Timeline / RCA 가 호출하는 통합 관측성 API.

설계서 §Q4:
    OTelQueryService: Incident Timeline 용 통합 API 제공

본 서비스는 Prometheus + Loki 어댑터를 결합하고, Incident Correlator 와 RCA Agent 가
한 곳에서 호출할 수 있도록 정규화된 데이터를 반환한다.

OTel 미연결 시에도 빈 결과 + `available=False` + `fallback_reason` 을 반환한다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .loki_adapter import LokiAdapter
from .prometheus_adapter import PrometheusAdapter

log = logging.getLogger(__name__)


@dataclass
class IncidentMetricSnapshot:
    """RCA 가 사용하는 정규화된 메트릭 스냅샷."""

    service: str
    error_rate_now: Optional[float] = None
    error_rate_baseline: Optional[float] = None
    latency_p95_now: Optional[float] = None
    latency_p95_baseline: Optional[float] = None
    restart_count_recent: Optional[float] = None
    memory_bytes: Optional[float] = None
    available: bool = False
    fallback_reason: Optional[str] = None


class OTelQueryService:
    """Prometheus + Loki 어댑터를 결합한 통합 인터페이스.

    환경변수 (모두 옵션):
        PROMETHEUS_URL, PROMETHEUS_TOKEN
        LOKI_URL,       LOKI_TOKEN
    """

    def __init__(
        self,
        prometheus: Optional[PrometheusAdapter] = None,
        loki: Optional[LokiAdapter] = None,
    ) -> None:
        self.prometheus = prometheus or PrometheusAdapter(
            base_url=os.environ.get("PROMETHEUS_URL"),
            bearer_token=os.environ.get("PROMETHEUS_TOKEN"),
        )
        self.loki = loki or LokiAdapter(
            base_url=os.environ.get("LOKI_URL"),
            bearer_token=os.environ.get("LOKI_TOKEN"),
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """둘 중 하나라도 base_url 이 잡혀 있으면 True."""
        return bool(self.prometheus.base_url or self.loki.base_url)

    # ------------------------------------------------------------------
    # Snapshot — Incident Correlator / RCA 가 호출하는 1차 entry point
    # ------------------------------------------------------------------

    def snapshot_for_service(
        self,
        service: str,
        container_name: Optional[str] = None,
        window: str = "5m",
    ) -> IncidentMetricSnapshot:
        snap = IncidentMetricSnapshot(service=service)

        if not self.available():
            snap.available = False
            snap.fallback_reason = "no observability backend configured"
            return snap

        snap.available = True

        # error rate
        now = self.prometheus.error_rate(service=service, window=window)
        snap.error_rate_now = _first_value(now)

        baseline = self.prometheus.error_rate(service=service, window="30m")
        snap.error_rate_baseline = _first_value(baseline)

        # latency p95
        lat_now = self.prometheus.latency_p95(service=service, window=window)
        snap.latency_p95_now = _first_value(lat_now)
        lat_base = self.prometheus.latency_p95(service=service, window="30m")
        snap.latency_p95_baseline = _first_value(lat_base)

        if container_name:
            restarts = self.prometheus.container_restart_count(container_name, window="10m")
            snap.restart_count_recent = _first_value(restarts)
            mem = self.prometheus.memory_usage_bytes(container_name)
            snap.memory_bytes = _first_value(mem)

        return snap

    # ------------------------------------------------------------------
    # Log excerpt
    # ------------------------------------------------------------------

    def container_error_excerpt(
        self,
        container_name: str,
        *,
        minutes: int = 15,
        keyword: str = "error",
        limit: int = 50,
    ) -> list[str]:
        result = self.loki.container_errors(
            container_name=container_name,
            minutes=minutes,
            keyword=keyword,
            limit=limit,
        )
        return [s["line"] for s in result.samples if "line" in s][:limit]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_value(result: Any) -> Optional[float]:
    samples = getattr(result, "samples", []) or []
    if not samples:
        return None
    v = samples[0].get("value")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["OTelQueryService", "IncidentMetricSnapshot"]
