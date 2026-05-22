"""
core.observability._legacy_manager — 패키지화 호환 어댑터.

설계서 §Q4 OTel + Prometheus 통합 매니저.

기존 `core/observability.py` 파일이 패키지 `core/observability/` 와 충돌하던 문제를
해결하기 위해 이 모듈로 이전했다. `from core.observability import observability`
형태의 기존 import 가 그대로 동작하도록 `__init__.py` 에서 re-export 한다.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Optional

from core.schemas import MetricPoint, ObservabilityConfig

logger = logging.getLogger(__name__)

# OTel 라이브러리 선택적 임포트 (미설치 환경 대응)
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    logger.warning("opentelemetry-sdk not installed — tracing disabled")

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False
    logger.warning("prometheus-client not installed — metrics disabled")

try:
    import httpx as _httpx  # noqa: F401
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


# ---------------------------------------------------------------------------
# Prometheus 메트릭 정의 (있을 때만)
# ---------------------------------------------------------------------------

if _PROMETHEUS_AVAILABLE:
    _deploy_total = Counter(
        "recoder_deployments_total",
        "Total ECS/ArgoCD deployments",
        ["project_id", "status", "type"],
    )
    _deploy_duration = Histogram(
        "recoder_deployment_duration_seconds",
        "Deployment duration in seconds",
        ["project_id", "type"],
        buckets=[30, 60, 120, 300, 600, 1200],
    )
    _incident_total = Counter(
        "recoder_incidents_total",
        "Total incidents opened",
        ["project_id", "severity"],
    )
    _policy_eval_total = Counter(
        "recoder_policy_evaluations_total",
        "Total OPA policy evaluations",
        ["decision"],
    )
    _active_deployments = Gauge(
        "recoder_active_deployments",
        "Currently in-progress deployments",
    )
else:
    _deploy_total = None
    _deploy_duration = None
    _incident_total = None
    _policy_eval_total = None
    _active_deployments = None


class ObservabilityManager:
    """OTel Tracer + Prometheus 메트릭 + Loki 로그 통합 관리자."""

    def __init__(self, config: Optional[ObservabilityConfig] = None) -> None:
        self.config = config or ObservabilityConfig()
        self._tracer = None
        self._initialized = False

    def initialize(self) -> None:
        """OTel 초기화. FastAPI 앱 startup 에서 호출."""
        if not self.config.enabled:
            logger.info("Observability disabled by config")
            return
        if _OTEL_AVAILABLE:
            self._setup_tracer()
        if _PROMETHEUS_AVAILABLE:
            self._setup_prometheus()
        self._initialized = True
        logger.info(
            "Observability initialized: otel=%s prometheus=%s",
            _OTEL_AVAILABLE, _PROMETHEUS_AVAILABLE,
        )

    def _setup_tracer(self) -> None:
        resource = Resource.create({
            "service.name": self.config.service_name,
            "service.version": self.config.service_version,
        })
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=self.config.otel_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        self._tracer = trace.get_tracer(self.config.service_name)
        logger.info("OTel tracer configured: endpoint=%s", self.config.otel_endpoint)

    def _setup_prometheus(self) -> None:
        try:
            start_http_server(self.config.prometheus_port)
            logger.info("Prometheus metrics server started: port=%d", self.config.prometheus_port)
        except OSError as exc:
            logger.warning("Prometheus server already running or port busy: %s", exc)

    # 배포 메트릭 -------------------------------------------------------

    def record_deployment_start(self, project_id: str, deploy_type: str = "ecs") -> float:
        if _PROMETHEUS_AVAILABLE and _active_deployments:
            _active_deployments.inc()
        return time.monotonic()

    def record_deployment_end(
        self,
        project_id: str,
        start_time: float,
        status: str,
        deploy_type: str = "ecs",
    ) -> None:
        duration = time.monotonic() - start_time
        if _PROMETHEUS_AVAILABLE:
            if _deploy_total:
                _deploy_total.labels(
                    project_id=project_id, status=status, type=deploy_type
                ).inc()
            if _deploy_duration:
                _deploy_duration.labels(project_id=project_id, type=deploy_type).observe(duration)
            if _active_deployments:
                _active_deployments.dec()
        logger.info(
            "Deployment metric: project=%s type=%s status=%s duration=%.1fs",
            project_id, deploy_type, status, duration,
        )

    # 장애 / 정책 메트릭 ------------------------------------------------

    def record_incident(self, project_id: str, severity: str) -> None:
        if _PROMETHEUS_AVAILABLE and _incident_total:
            _incident_total.labels(project_id=project_id, severity=severity).inc()
        logger.info("Incident metric: project=%s severity=%s", project_id, severity)

    def record_policy_evaluation(self, decision: str) -> None:
        if _PROMETHEUS_AVAILABLE and _policy_eval_total:
            _policy_eval_total.labels(decision=decision).inc()

    # Loki push ---------------------------------------------------------

    async def push_log_to_loki(
        self,
        message: str,
        labels: dict,
        level: str = "info",
    ) -> None:
        if not _HTTPX_AVAILABLE:
            return
        ns_timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1_000_000_000))
        payload = {
            "streams": [
                {
                    "stream": {
                        "app": "recoder",
                        "level": level,
                        **labels,
                    },
                    "values": [[ns_timestamp, message]],
                }
            ]
        }
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(
                    f"{self.config.loki_url}/loki/api/v1/push",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code not in (200, 204):
                    logger.debug("Loki push failed: %d %s", resp.status_code, resp.text[:100])
        except Exception as exc:  # noqa: BLE001
            logger.debug("Loki push error (non-critical): %s", exc)

    # OTel span context -------------------------------------------------

    @contextmanager
    def span(self, name: str, attributes: Optional[dict] = None) -> Generator:
        if self._tracer and _OTEL_AVAILABLE:
            with self._tracer.start_as_current_span(name) as otel_span:
                if attributes:
                    for k, v in attributes.items():
                        otel_span.set_attribute(k, str(v))
                yield otel_span
        else:
            yield None

    def get_metrics_snapshot(self) -> list[MetricPoint]:
        points = []
        now = datetime.now(timezone.utc)
        if _PROMETHEUS_AVAILABLE:
            points.append(MetricPoint(
                name="recoder_observability_status",
                value=1.0 if self._initialized else 0.0,
                labels={"otel": str(_OTEL_AVAILABLE), "prometheus": str(_PROMETHEUS_AVAILABLE)},
                timestamp=now,
            ))
        return points


# 모듈 레벨 싱글톤
observability = ObservabilityManager()
