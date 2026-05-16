"""
core.observability — ReCoder v5.0 Q4 OpenTelemetry 통합 패키지.

설계서 §Q4 OpenTelemetry 통합 항목 구현:
  - otel_adapter        : Collector 설정 빌더 / Local Core LLM 호출 Span 계측
  - prometheus_adapter  : PromQL 메트릭 쿼리 (error rate, latency, CPU/Mem)
  - loki_adapter        : LogQL 로그 쿼리, 컨테이너 로그 발췌
  - otel_query_service  : Incident Timeline / RCA 가 사용하는 통합 API
  - tempo_adapter       : (Q4 후반) trace 쿼리 — Q4 1차에는 placeholder

원칙:
  - 외부 백엔드 (Prometheus / Loki / Tempo) 미연결 시에도 import / 호출은 실패하지
    않는다. 결과의 `error` 필드와 `backend="fallback"` 으로 명확히 표시한다.
  - 어떤 모듈도 raw source code / 시크릿 / .env 값을 직접 전달하지 않는다 (ADR-004).
"""

from .otel_adapter import (
    OTelCollectorConfig,
    build_otel_collector_config,
    instrument_llm_span,
    instrument_wedge_trace,
)
from .prometheus_adapter import PrometheusAdapter
from .loki_adapter import LokiAdapter
from .otel_query_service import OTelQueryService

__all__ = [
    "OTelCollectorConfig",
    "build_otel_collector_config",
    "instrument_llm_span",
    "instrument_wedge_trace",
    "PrometheusAdapter",
    "LokiAdapter",
    "OTelQueryService",
]
