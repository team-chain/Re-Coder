"""
Local Core — Q4: OpenTelemetry 연동

설계서 §Q4-A (Must):
- OTel Collector → Prometheus 메트릭 + Loki 로그
- 배포/장애/RCA 이벤트를 메트릭/트레이스로 기록
- FastAPI 미들웨어로 자동 HTTP 트레이싱

의존성 (선택적 임포트):
- opentelemetry-sdk
- opentelemetry-exporter-otlp-proto-grpc
- opentelemetry-instrumentation-fastapi
- prometheus-client
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Optional

from core.schemas import MetricPoint, ObservabilityConfig, TraceSpan

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
    import httpx as _httpx
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
    """
    OTel Tracer + Prometheus 메트릭 + Loki 로그 통합 관리자.
    """

    def __init__(self, config: Optional[ObservabilityConfig] = None) -> None:
        self.config = config or ObservabilityConfig()
        self._tracer = None
        self._initialized = False

    def initialize(self) -> None:
        """OTel 초기화. FastAPI 앱 startup에서 호출."""
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

    # ------------------------------------------------------------------
    # 배포 메트릭
    # ------------------------------------------------------------------

    def record_deployment_start(self, project_id: str, deploy_type: str = "ecs") -> float:
        """배포 시작 시각 기록. 반환값을 record_deployment_end에 전달."""
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
        """배포 완료 메트릭 기록."""
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

    # ------------------------------------------------------------------
    # 장애 메트릭
    # ------------------------------------------------------------------

    def record_incident(self, project_id: str, severity: str) -> None:
        """장애 발생 메트릭."""
        if _PROMETHEUS_AVAILABLE and _incident_total:
            _incident_total.labels(project_id=project_id, severity=severity).inc()
        logger.info("Incident metric: project=%s severity=%s", project_id, severity)

    # ------------------------------------------------------------------
    # 정책 평가 메트릭
    # ------------------------------------------------------------------

    def record_policy_evaluation(self, decision: str) -> None:
        """OPA 정책 평가 결과 메트릭."""
        if _PROMETHEUS_AVAILABLE and _policy_eval_total:
            _policy_eval_total.labels(decision=decision).inc()

    # ------------------------------------------------------------------
    # Loki 로그 push (HTTP)
    # ------------------------------------------------------------------

    async def push_log_to_loki(
        self,
        message: str,
        labels: dict,
        level: str = "info",
    ) -> None:
        """Loki push API로 로그 전송."""
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
        except Exception as exc:
            logger.debug("Loki push error (non-critical): %s", exc)

    # ------------------------------------------------------------------
    # OTel 스팬 컨텍스트 매니저
    # ------------------------------------------------------------------

    @contextmanager
    def span(self, name: str, attributes: Optional[dict] = None) -> Generator:
        """OTel 스팬 컨텍스트 매니저."""
        if self._tracer and _OTEL_AVAILABLE:
            with self._tracer.start_as_current_span(name) as otel_span:
                if attributes:
                    for k, v in attributes.items():
                        otel_span.set_attribute(k, str(v))
                yield otel_span
        else:
            yield None

    def get_metrics_snapshot(self) -> list[MetricPoint]:
        """현재 메트릭 스냅샷 반환 (API 응답용)."""
        points = []
        now = datetime.now(timezone.utc)

        if _PROMETHEUS_AVAILABLE:
            # Prometheus 메트릭은 scrape 방식이므로 여기서는 기본값만 반환
            points.append(MetricPoint(
                name="recoder_observability_status",
                value=1.0 if self._initialized else 0.0,
                labels={"otel": str(_OTEL_AVAILABLE), "prometheus": str(_PROMETHEUS_AVAILABLE)},
                timestamp=now,
            ))

        return points


# 모듈 레벨 싱글톤
observability = ObservabilityManager()
