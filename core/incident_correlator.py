"""
incident_correlator.py — Incident ↔ Deployment 상관관계 계산 (설계서 §Q4).

설계서 명세 (Incident Correlation 설계):
    가장 최근 DeploymentRecord 와의 시간 근접성은 1차 후보일 뿐이다.
    다음 신호들을 함께 계산해서 correlation score 를 산출한다.
      - 배포 전후 error rate 변화
      - 배포 전후 latency 변화
      - 변경된 파일의 영역
      - container restart event 시점
      - health check failure 시점
      - log keyword 변화
      - traffic spike 여부
      - 외부 dependency error 여부

    correlation score 가 낮으면 "최근 배포와 직접 관련성 낮음" 으로 표시한다.

본 모듈은 위 8개 신호를 받아 가중 평균(0~1) correlation_score 와 label 을 산출한다.
관측 데이터가 없는 경우에도 에러를 던지지 않고 weak_link / no_link 라벨을 부여한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

try:
    from schemas import (
        CorrelationResult,
        CorrelationSignal,
        CorrelationSignalKind,
        DeploymentRecord,
    )
except ImportError:
    from core.schemas import (  # type: ignore
        CorrelationResult,
        CorrelationSignal,
        CorrelationSignalKind,
        DeploymentRecord,
    )


# ---------------------------------------------------------------------------
# 가중치 — 설계서에 명시 가중치는 없으므로 합 1.0 으로 정규화한 권장값.
# 조정 시 한 자리만 바꿔도 정규화는 자동.
# ---------------------------------------------------------------------------

_DEFAULT_WEIGHTS: dict[CorrelationSignalKind, float] = {
    CorrelationSignalKind.ERROR_RATE_DELTA:     0.22,
    CorrelationSignalKind.LATENCY_DELTA:        0.13,
    CorrelationSignalKind.CHANGED_FILES_AREA:   0.13,
    CorrelationSignalKind.CONTAINER_RESTART:    0.14,
    CorrelationSignalKind.HEALTH_CHECK_FAILURE: 0.14,
    CorrelationSignalKind.LOG_KEYWORD_DELTA:    0.10,
    CorrelationSignalKind.TRAFFIC_SPIKE:        0.06,
    CorrelationSignalKind.DEPENDENCY_ERROR:     0.08,
}


# ---------------------------------------------------------------------------
# Input dataclass — 호출 측이 채우는 raw 관측치
# ---------------------------------------------------------------------------


@dataclass
class CorrelationInput:
    """Incident Correlator 입력. 모든 필드 optional — 누락 신호는 점수 0 으로 처리."""

    incident_id: str
    detected_at: datetime
    candidate_deployment: Optional[DeploymentRecord] = None

    # 신호별 raw 관측치
    error_rate_before: Optional[float] = None  # 0~1
    error_rate_after: Optional[float] = None   # 0~1
    latency_before_ms: Optional[float] = None
    latency_after_ms: Optional[float] = None
    changed_files: list[str] = field(default_factory=list)
    affected_path_prefixes: list[str] = field(default_factory=list)
    container_restart_count: Optional[float] = None
    health_check_failed: Optional[bool] = None
    log_keyword_delta: dict[str, float] = field(default_factory=dict)
    traffic_rps_before: Optional[float] = None
    traffic_rps_after: Optional[float] = None
    dependency_errors: list[str] = field(default_factory=list)

    weights: dict[CorrelationSignalKind, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public — correlate
# ---------------------------------------------------------------------------


def correlate(inp: CorrelationInput) -> CorrelationResult:
    """8개 신호를 평가하고 CorrelationResult 를 반환한다."""

    weights = {**_DEFAULT_WEIGHTS, **(inp.weights or {})}
    total_weight = sum(weights.values()) or 1.0

    signals: list[CorrelationSignal] = []
    weighted_sum = 0.0
    contributing_signals = 0

    # 1) error rate delta
    sig = _error_rate_signal(inp.error_rate_before, inp.error_rate_after)
    if sig is not None:
        sig.weight = weights[CorrelationSignalKind.ERROR_RATE_DELTA] / total_weight
        signals.append(sig)
        weighted_sum += sig.weight * sig.score
        contributing_signals += 1

    # 2) latency delta
    sig = _latency_signal(inp.latency_before_ms, inp.latency_after_ms)
    if sig is not None:
        sig.weight = weights[CorrelationSignalKind.LATENCY_DELTA] / total_weight
        signals.append(sig)
        weighted_sum += sig.weight * sig.score
        contributing_signals += 1

    # 3) changed files area
    sig = _changed_files_signal(inp.changed_files, inp.affected_path_prefixes)
    if sig is not None:
        sig.weight = weights[CorrelationSignalKind.CHANGED_FILES_AREA] / total_weight
        signals.append(sig)
        weighted_sum += sig.weight * sig.score
        contributing_signals += 1

    # 4) container restart
    sig = _container_restart_signal(inp.container_restart_count)
    if sig is not None:
        sig.weight = weights[CorrelationSignalKind.CONTAINER_RESTART] / total_weight
        signals.append(sig)
        weighted_sum += sig.weight * sig.score
        contributing_signals += 1

    # 5) health check failure
    sig = _health_signal(inp.health_check_failed)
    if sig is not None:
        sig.weight = weights[CorrelationSignalKind.HEALTH_CHECK_FAILURE] / total_weight
        signals.append(sig)
        weighted_sum += sig.weight * sig.score
        contributing_signals += 1

    # 6) log keyword delta
    sig = _log_keyword_signal(inp.log_keyword_delta)
    if sig is not None:
        sig.weight = weights[CorrelationSignalKind.LOG_KEYWORD_DELTA] / total_weight
        signals.append(sig)
        weighted_sum += sig.weight * sig.score
        contributing_signals += 1

    # 7) traffic spike
    sig = _traffic_signal(inp.traffic_rps_before, inp.traffic_rps_after)
    if sig is not None:
        sig.weight = weights[CorrelationSignalKind.TRAFFIC_SPIKE] / total_weight
        signals.append(sig)
        weighted_sum += sig.weight * sig.score
        contributing_signals += 1

    # 8) dependency error
    sig = _dependency_signal(inp.dependency_errors)
    if sig is not None:
        sig.weight = weights[CorrelationSignalKind.DEPENDENCY_ERROR] / total_weight
        signals.append(sig)
        weighted_sum += sig.weight * sig.score
        contributing_signals += 1

    score = max(0.0, min(1.0, weighted_sum))

    # confidence: 신호 가짓수 / 전체 가능 신호(8) — 관측 데이터 부족 시 자연스럽게 낮아진다
    confidence = round(contributing_signals / 8.0, 3)

    label, rationale = _label_for_score(score, contributing_signals)

    return CorrelationResult(
        incident_id=inp.incident_id,
        candidate_deployment_id=(
            inp.candidate_deployment.deployment_id if inp.candidate_deployment else None
        ),
        candidate_image_tag=(
            inp.candidate_deployment.image if inp.candidate_deployment else None
        ),
        signals=signals,
        correlation_score=round(score, 3),
        confidence=confidence,
        label=label,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# 신호별 개별 평가 함수 (None 반환 시 신호 미관측)
# ---------------------------------------------------------------------------


def _error_rate_signal(before: Optional[float], after: Optional[float]) -> Optional[CorrelationSignal]:
    if before is None or after is None:
        return None
    delta = max(0.0, after - before)
    score = min(1.0, delta / 0.05)  # 5%p 증가 = 1.0
    return CorrelationSignal(
        kind=CorrelationSignalKind.ERROR_RATE_DELTA,
        weight=0.0,
        score=score,
        evidence=f"error_rate {before:.3f} -> {after:.3f} (Δ={delta:.3f})",
        raw={"before": before, "after": after},
    )


def _latency_signal(before: Optional[float], after: Optional[float]) -> Optional[CorrelationSignal]:
    if before is None or after is None or before <= 0:
        return None
    ratio = max(0.0, (after - before) / before)
    score = min(1.0, ratio / 1.0)  # 100% 증가 = 1.0
    return CorrelationSignal(
        kind=CorrelationSignalKind.LATENCY_DELTA,
        weight=0.0,
        score=score,
        evidence=f"latency_ms {before:.1f} -> {after:.1f} (Δratio={ratio:.2f})",
        raw={"before": before, "after": after},
    )


def _changed_files_signal(
    changed_files: list[str],
    affected_prefixes: list[str],
) -> Optional[CorrelationSignal]:
    if not changed_files:
        return None
    if not affected_prefixes:
        score = 0.4  # 변경은 있으나 영향 영역 정보 없음 — 약한 양성
        evidence = (
            f"{len(changed_files)} files changed; affected path prefixes unknown"
        )
    else:
        hits = sum(
            1
            for f in changed_files
            for p in affected_prefixes
            if f.startswith(p)
        )
        ratio = hits / max(1, len(changed_files))
        score = min(1.0, ratio)
        evidence = (
            f"{hits}/{len(changed_files)} changed files match affected prefixes"
        )
    return CorrelationSignal(
        kind=CorrelationSignalKind.CHANGED_FILES_AREA,
        weight=0.0,
        score=score,
        evidence=evidence,
        raw={"changed_files": changed_files, "affected_prefixes": affected_prefixes},
    )


def _container_restart_signal(count: Optional[float]) -> Optional[CorrelationSignal]:
    if count is None:
        return None
    score = 0.0 if count <= 0 else min(1.0, count / 3.0)  # 3회 = 1.0
    return CorrelationSignal(
        kind=CorrelationSignalKind.CONTAINER_RESTART,
        weight=0.0,
        score=score,
        evidence=f"container restarts in last 10m: {count}",
        raw={"count": count},
    )


def _health_signal(failed: Optional[bool]) -> Optional[CorrelationSignal]:
    if failed is None:
        return None
    score = 1.0 if failed else 0.0
    return CorrelationSignal(
        kind=CorrelationSignalKind.HEALTH_CHECK_FAILURE,
        weight=0.0,
        score=score,
        evidence=f"health_check_failed={failed}",
        raw={"failed": failed},
    )


def _log_keyword_signal(delta: dict[str, float]) -> Optional[CorrelationSignal]:
    if not delta:
        return None
    # 가장 큰 증가 비율을 사용 (0~1 클리핑)
    max_delta = max(delta.values())
    score = max(0.0, min(1.0, max_delta))
    top = sorted(delta.items(), key=lambda kv: kv[1], reverse=True)[:3]
    return CorrelationSignal(
        kind=CorrelationSignalKind.LOG_KEYWORD_DELTA,
        weight=0.0,
        score=score,
        evidence="top keyword delta: " + ", ".join(f"{k}={v:.2f}" for k, v in top),
        raw={"delta": delta},
    )


def _traffic_signal(before: Optional[float], after: Optional[float]) -> Optional[CorrelationSignal]:
    if before is None or after is None or before <= 0:
        return None
    spike_ratio = max(0.0, (after - before) / before)
    # 트래픽 스파이크 자체는 약한 신호 — 2배까지는 자연 변동이라 가정
    score = max(0.0, min(1.0, (spike_ratio - 1.0) / 2.0))
    return CorrelationSignal(
        kind=CorrelationSignalKind.TRAFFIC_SPIKE,
        weight=0.0,
        score=score,
        evidence=f"traffic rps {before:.1f} -> {after:.1f}",
        raw={"before": before, "after": after},
    )


def _dependency_signal(errors: list[str]) -> Optional[CorrelationSignal]:
    if not errors:
        return None
    # 외부 의존성 에러가 있으면 score 를 올리되, "배포와의 인과"는 약하므로 캡 0.6
    score = min(0.6, 0.2 + 0.2 * len(errors))
    return CorrelationSignal(
        kind=CorrelationSignalKind.DEPENDENCY_ERROR,
        weight=0.0,
        score=score,
        evidence=f"{len(errors)} external dependency errors observed",
        raw={"errors": errors[:5]},
    )


# ---------------------------------------------------------------------------
# Labeling — 점수 → 사람이 읽는 라벨
# ---------------------------------------------------------------------------


def _label_for_score(score: float, contributing: int) -> tuple[str, str]:
    if contributing == 0:
        return (
            "no_link",
            "관측 신호가 부족합니다. 최근 배포와의 직접 관련성을 판단할 수 없습니다.",
        )
    if score >= 0.6:
        return (
            "candidate",
            "최근 배포가 인시던트의 유력한 후보입니다.",
        )
    if score >= 0.3:
        return (
            "weak_link",
            "최근 배포와 약한 관련성이 관측됩니다. 추가 검증이 필요합니다.",
        )
    return (
        "no_link",
        "최근 배포와 직접 관련성이 낮습니다.",
    )


__all__ = [
    "CorrelationInput",
    "correlate",
]
