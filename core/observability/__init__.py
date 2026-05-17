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

# ---------------------------------------------------------------------------
# Legacy compatibility — core/observability.py 의 `observability` 싱글톤이
# `from core.observability import observability` 로 import 되는 경로를 보존한다.
# 패키지화 후에도 기존 import 가 깨지지 않도록 한다.
# ---------------------------------------------------------------------------

try:  # pragma: no cover — 환경에 따라 누락 가능
    # NOTE: core/observability.py 와 core/observability/ 가 공존할 때
    # Python 은 패키지(__init__.py) 를 우선한다. 레거시 manager 는
    # 동일 디렉터리의 _legacy_manager.py 로 이전했거나, 없으면 새로 만든다.
    from ._legacy_manager import ObservabilityManager, observability  # type: ignore
except ImportError:  # pragma: no cover
    ObservabilityManager = None  # type: ignore
    observability = None  # type: ignore

__all__ = [
    "OTelCollectorConfig",
    "build_otel_collector_config",
    "instrument_llm_span",
    "instrument_wedge_trace",
    "PrometheusAdapter",
    "LokiAdapter",
    "OTelQueryService",
    "ObservabilityManager",
    "observability",
]
