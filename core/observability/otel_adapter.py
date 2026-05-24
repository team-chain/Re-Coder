"""
otel_adapter.py — OpenTelemetry Collector 설정 빌더 + Local Core 계측 (설계서 §Q4).

설계서 명세 (요지):
  - Watchdog v2 의 Fluent Bit 을 OTel Collector 로 대체한다.
  - EC2 에서 컨테이너로 실행하고 docker-compose.yml 에 Watchdog 과 함께 정의한다.
  - 수집 파이프라인:
        Receiver  : otlp (grpc:4317, http:4318), docker_stats, filelog
        Processor : batch, memory_limiter, resource_detection
        Exporter  : Prometheus remote_write, Loki   (Tempo 는 Q4 후반)
  - Local Core 의 LLM 호출마다 OTel Span 을 생성한다. attributes:
        provider, model, input_tokens, output_tokens, cost_usd, operation
  - 쐐기 시나리오 7단계가 하나의 Trace 로 연결된다.

본 모듈은 다음 두 역할을 한다.
  1. OTel Collector 설정 YAML 을 코드로 빌드한다 (테스트 가능한 dict 기반).
  2. opentelemetry-api 가 설치돼 있다면 LLM 호출 / wedge 단계마다 Span 을 만든다.
     설치돼 있지 않으면 no-op context manager 를 반환한다 (런타임 안전).
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional OTel import — 미설치 환경에서도 안전하게 동작
# ---------------------------------------------------------------------------

try:
    from opentelemetry import trace as _otel_trace  # type: ignore
    from opentelemetry.trace import Status, StatusCode  # type: ignore

    _OTEL_AVAILABLE = True
except Exception:  # pragma: no cover - 운영 환경 의존성
    _otel_trace = None  # type: ignore
    Status = None  # type: ignore
    StatusCode = None  # type: ignore
    _OTEL_AVAILABLE = False


def is_otel_available() -> bool:
    return _OTEL_AVAILABLE


# ---------------------------------------------------------------------------
# Collector config builder
# ---------------------------------------------------------------------------


@dataclass
class OTelCollectorConfig:
    """OTel Collector 설정 빌더 입력 (설계서 §Q4 1차 exporter — Prom + Loki)."""

    service_name: str = "recoder-core"
    grpc_endpoint: str = "0.0.0.0:4317"
    http_endpoint: str = "0.0.0.0:4318"
    enable_docker_stats: bool = True
    docker_endpoint: str = "unix:///var/run/docker.sock"
    enable_filelog: bool = True
    filelog_paths: tuple[str, ...] = (
        "/var/log/recoder/*.log",
        "/var/lib/docker/containers/*/*-json.log",
    )
    prometheus_remote_write_url: Optional[str] = None
    loki_endpoint: Optional[str] = None
    enable_tempo: bool = False  # Q4 후반
    tempo_endpoint: Optional[str] = None
    memory_limit_mib: int = 512
    batch_timeout: str = "5s"
    extra_resource_attrs: dict[str, str] = field(default_factory=dict)


def build_otel_collector_config(cfg: OTelCollectorConfig) -> dict[str, Any]:
    """OTel Collector YAML 에 직렬화 가능한 dict 를 반환.

    Receiver → Processor → Exporter → Service 순으로 구성된다.
    Tempo 가 비활성화돼 있어도 dict 자체는 안정적으로 생성된다.
    """

    # ── receivers ──
    receivers: dict[str, Any] = {
        "otlp": {
            "protocols": {
                "grpc": {"endpoint": cfg.grpc_endpoint},
                "http": {"endpoint": cfg.http_endpoint},
            }
        }
    }
    if cfg.enable_docker_stats:
        receivers["docker_stats"] = {
            "endpoint": cfg.docker_endpoint,
            "collection_interval": "30s",
            "timeout": "10s",
        }
    if cfg.enable_filelog:
        receivers["filelog"] = {
            "include": list(cfg.filelog_paths),
            "start_at": "end",
            "include_file_path": True,
        }

    # ── processors ──
    resource_attrs = {
        "service.name": cfg.service_name,
        "deployment.environment": os.environ.get("RECODER_ENV", "dev"),
    }
    resource_attrs.update(cfg.extra_resource_attrs)
    processors: dict[str, Any] = {
        "memory_limiter": {
            "check_interval": "2s",
            "limit_mib": cfg.memory_limit_mib,
        },
        "batch": {"timeout": cfg.batch_timeout},
        "resourcedetection": {
            "detectors": ["env", "system"],
            "timeout": "5s",
            "override": False,
        },
        "attributes/recoder": {
            "actions": [
                {"key": k, "value": v, "action": "upsert"}
                for k, v in resource_attrs.items()
            ]
        },
    }

    # ── exporters ──
    exporters: dict[str, Any] = {}
    metric_exporters: list[str] = []
    log_exporters: list[str] = []
    trace_exporters: list[str] = []

    if cfg.prometheus_remote_write_url:
        exporters["prometheusremotewrite"] = {
            "endpoint": cfg.prometheus_remote_write_url,
            "tls": {"insecure": True},
        }
        metric_exporters.append("prometheusremotewrite")
    if cfg.loki_endpoint:
        exporters["loki"] = {"endpoint": cfg.loki_endpoint}
        log_exporters.append("loki")
    if cfg.enable_tempo and cfg.tempo_endpoint:
        exporters["otlp/tempo"] = {
            "endpoint": cfg.tempo_endpoint,
            "tls": {"insecure": True},
        }
        trace_exporters.append("otlp/tempo")

    # 항상 debug exporter 를 추가해 미연결 환경에서도 파이프라인이 살아 있게 한다
    exporters["debug"] = {"verbosity": "basic"}
    if not metric_exporters:
        metric_exporters.append("debug")
    if not log_exporters:
        log_exporters.append("debug")
    if not trace_exporters:
        trace_exporters.append("debug")

    # ── service / pipelines ──
    service = {
        "telemetry": {"logs": {"level": "info"}},
        "pipelines": {
            "metrics": {
                "receivers": ["otlp"] + (
                    ["docker_stats"] if cfg.enable_docker_stats else []
                ),
                "processors": [
                    "memory_limiter",
                    "resourcedetection",
                    "attributes/recoder",
                    "batch",
                ],
                "exporters": metric_exporters,
            },
            "logs": {
                "receivers": ["otlp"] + (
                    ["filelog"] if cfg.enable_filelog else []
                ),
                "processors": [
                    "memory_limiter",
                    "resourcedetection",
                    "attributes/recoder",
                    "batch",
                ],
                "exporters": log_exporters,
            },
            "traces": {
                "receivers": ["otlp"],
                "processors": [
                    "memory_limiter",
                    "resourcedetection",
                    "attributes/recoder",
                    "batch",
                ],
                "exporters": trace_exporters,
            },
        },
    }

    return {
        "receivers": receivers,
        "processors": processors,
        "exporters": exporters,
        "service": service,
    }


# ---------------------------------------------------------------------------
# Local Core 계측 — Span context manager
# ---------------------------------------------------------------------------


def _tracer():
    if not _OTEL_AVAILABLE:
        return None
    return _otel_trace.get_tracer("recoder.core")  # type: ignore[attr-defined]


@contextlib.contextmanager
def instrument_llm_span(
    *,
    provider: str,
    model: str,
    operation: str,
    extra_attrs: Optional[dict[str, Any]] = None,
) -> Iterator[Any]:
    """LLM 호출 한 건을 감싸는 Span context manager.

    설계서 §Q4 attributes 명세:
        provider, model, input_tokens, output_tokens, cost_usd, operation
    `input_tokens`, `output_tokens`, `cost_usd` 는 호출 종료 후 호출 측에서
    `set_attribute` 를 통해 채워넣는다 (Span 객체를 yield 한다).
    """

    tracer = _tracer()
    if tracer is None:
        # OTel 미설치: no-op
        class _NoopSpan:
            def set_attribute(self, *_args, **_kwargs):  # noqa: D401
                return None

            def set_status(self, *_args, **_kwargs):
                return None

            def record_exception(self, *_args, **_kwargs):
                return None

        yield _NoopSpan()
        return

    span_name = f"llm.{operation}"
    with tracer.start_as_current_span(span_name) as span:
        span.set_attribute("llm.provider", provider)
        span.set_attribute("llm.model", model)
        span.set_attribute("llm.operation", operation)
        for k, v in (extra_attrs or {}).items():
            try:
                span.set_attribute(f"llm.{k}", v)
            except Exception:  # noqa: BLE001
                # OTel 은 일부 타입만 attribute 로 받는다. 실패는 무시한다.
                log.debug("otel attribute skip: %s", k)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            if Status is not None and StatusCode is not None:  # pragma: no branch
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


@contextlib.contextmanager
def instrument_wedge_trace(stage: str, incident_id: Optional[str] = None) -> Iterator[Any]:
    """쐐기 시나리오 7단계가 하나의 Trace 로 묶이도록 Span 을 잡는다.

    `stage` 예시: "detect", "timeline", "rca", "rollback_pr", "approval",
                  "argocd_sync", "postmortem"
    """

    tracer = _tracer()
    if tracer is None:
        class _NoopSpan:
            def set_attribute(self, *_a, **_k):
                return None

        yield _NoopSpan()
        return

    span_name = f"wedge.{stage}"
    with tracer.start_as_current_span(span_name) as span:
        span.set_attribute("wedge.stage", stage)
        if incident_id:
            span.set_attribute("wedge.incident_id", incident_id)
        yield span


__all__ = [
    "OTelCollectorConfig",
    "build_otel_collector_config",
    "instrument_llm_span",
    "instrument_wedge_trace",
    "is_otel_available",
]
