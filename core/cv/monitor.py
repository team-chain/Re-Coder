"""
Continuous Verification Monitor (§34).

배포 직후 일정 시간 (default 5분) 동안 컨테이너를 감시하며 health/error/memory
지표를 누적해 자동 rollback 결정을 내린다.

설계:
  - 단위 폴링은 동기 호출 — 외부 의존 (docker CLI / HTTP probe) 이 짧음
  - 전체 5분 모니터링은 ``run_cv_sync()`` 동기 진입점, 또는 ``run_cv_async()``
    백그라운드 thread 진입점
  - 짧은 단위 테스트를 위해 metrics 수집은 hook 으로 주입 가능
  - 호출자가 contract 의 duration / interval / error_log_threshold 를 파싱
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    from preflight.runtime import http_probe, parse_duration
    from schemas import (
        CVResult,
        CVResultStatus,
        ContractContinuousVerification,
        ReleaseContract,
    )
except ImportError:  # pragma: no cover
    from core.preflight.runtime import http_probe, parse_duration  # type: ignore
    from core.schemas import (  # type: ignore
        CVResult,
        CVResultStatus,
        ContractContinuousVerification,
        ReleaseContract,
    )

from .triggers import AutoRollbackDecision, evaluate_triggers


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Observation snapshot
# ---------------------------------------------------------------------------


@dataclass
class CVObservation:
    """단일 polling tick 의 관찰값."""
    health_ok:          bool = False
    error_log_count:    int = 0          # 이 tick 동안 발생한 신규 에러 라인 수
    memory_pct:         float = 0.0      # 0~1
    notes:              list[str] = field(default_factory=list)


# Metrics provider — 테스트에서 mock 가능한 hook.
# Returns: (health_ok, error_log_count_since_last, memory_pct)
MetricsProvider = Callable[[float], CVObservation]


# ---------------------------------------------------------------------------
# Error rate 임계값 파싱 — "10/min", "5/sec" 같은 contract string
# ---------------------------------------------------------------------------


_RATE_RE: re.Pattern[str] = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*/\s*(s|sec|second|m|min|minute|h|hour)\s*$",
    re.IGNORECASE,
)


def parse_error_log_threshold(s: str | None) -> float:
    """``"10/min"`` → 10.0 (per minute). ``"30/sec"`` → 1800.0 (per minute).

    못 읽으면 기본 10.0 /min.
    """
    if not s:
        return 10.0
    m = _RATE_RE.match(str(s))
    if not m:
        return 10.0
    n = float(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("s"):
        return n * 60.0
    if unit.startswith("h"):
        return n / 60.0
    return n  # default min


# ---------------------------------------------------------------------------
# Default metrics provider — docker CLI 기반
# ---------------------------------------------------------------------------


@dataclass
class DockerMetricsCollector:
    """docker stats + docker logs 기반 기본 metrics provider.

    Args:
        container_id:  감시 대상 컨테이너
        host_port:     health probe 용 호스트 포트
        health_path:   health endpoint 경로
        cli:           docker CLI 명령어 (기본 'docker')
    """
    container_id:  str
    host_port:     int
    health_path:   str = "/health"
    cli:           str = "docker"
    _last_log_ts:  float = field(default=0.0)

    def __call__(self, tick_duration_seconds: float) -> CVObservation:
        obs = CVObservation()

        # 1) health
        ok, _, _ = http_probe("127.0.0.1", self.host_port, path=self.health_path, timeout=2.0)
        obs.health_ok = ok

        # 2) error logs — docker logs --since <last_ts>
        since = max(0.0, tick_duration_seconds + 1.0)  # tick + 1s 여유
        try:
            res = subprocess.run(
                [self.cli, "logs", "--since", f"{int(since)}s", self.container_id],
                capture_output=True, text=True, timeout=5,
            )
            lines = (res.stdout + res.stderr).splitlines()
            obs.error_log_count = sum(
                1 for line in lines
                if _looks_like_error_line(line)
            )
        except subprocess.TimeoutExpired:
            obs.notes.append("docker logs timeout")
        except OSError as exc:
            obs.notes.append(f"docker logs failed: {exc}")

        # 3) memory % — docker stats --no-stream
        try:
            res2 = subprocess.run(
                [self.cli, "stats", "--no-stream", "--format", "{{.MemPerc}}",
                 self.container_id],
                capture_output=True, text=True, timeout=5,
            )
            raw = res2.stdout.strip()
            obs.memory_pct = _parse_mem_percent(raw)
        except subprocess.TimeoutExpired:
            obs.notes.append("docker stats timeout")
        except OSError as exc:
            obs.notes.append(f"docker stats failed: {exc}")

        return obs


_ERROR_LINE_RE: re.Pattern[str] = re.compile(
    r"\b(?:ERROR|FATAL|CRITICAL|Exception|Traceback|PANIC)\b",
    re.IGNORECASE,
)


def _looks_like_error_line(line: str) -> bool:
    if not line:
        return False
    return bool(_ERROR_LINE_RE.search(line))


def _parse_mem_percent(raw: str) -> float:
    """``"23.45%"`` → 0.2345.  여러 줄 / 빈 입력은 0.0."""
    if not raw:
        return 0.0
    line = raw.splitlines()[0].strip().rstrip("%")
    try:
        return float(line) / 100.0
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# CV Monitor
# ---------------------------------------------------------------------------


class CVMonitor:
    """
    Continuous Verification Monitor.

    Args:
        deployment_id:    감시 대상 배포 식별자 (DeploymentLedger.deployment_id)
        contract:         ReleaseContract — duration / interval / threshold / triggers 읽음
        metrics_provider: 매 polling tick 에 호출될 함수. None 이면 DockerMetricsCollector.
        max_duration:     contract 설정과 별도로 강제 상한 (테스트 안전망)
    """

    def __init__(
        self,
        deployment_id: str,
        contract: ReleaseContract,
        metrics_provider: Optional[MetricsProvider] = None,
        *,
        max_duration_seconds: Optional[int] = None,
    ) -> None:
        self.deployment_id = deployment_id
        self.contract = contract
        self.metrics_provider = metrics_provider
        self.max_duration_seconds = max_duration_seconds
        self._stop = threading.Event()

    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """외부에서 중단 시그널."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------------

    def run(self) -> CVResult:
        """동기 실행 — 끝까지 polling 또는 ``request_stop`` 까지."""
        cfg: ContractContinuousVerification = self.contract.operational_policy.continuous_verification
        duration_s = parse_duration(cfg.duration, default=300)
        interval_s = parse_duration(cfg.health_check_interval, default=30)
        threshold_per_min = parse_error_log_threshold(cfg.error_log_threshold)

        if self.max_duration_seconds is not None:
            duration_s = min(duration_s, self.max_duration_seconds)

        provider = self.metrics_provider
        if provider is None:
            # docker 기반 기본 provider — host_port, container_id 등 필요. 호출자가 명시 안 한 경우
            # NULL provider 로 빈 observation 반환 (test 환경 안전망).
            provider = _null_provider

        health_failures = 0
        total_error_lines = 0
        max_mem_pct = 0.0
        ticks: list[CVObservation] = []
        notes: list[str] = []

        t_start = time.monotonic()
        deadline = t_start + duration_s
        last_tick = t_start
        while not self._stop.is_set() and time.monotonic() < deadline:
            t_before = time.monotonic()
            obs = provider(t_before - last_tick)
            last_tick = t_before
            ticks.append(obs)
            if not obs.health_ok:
                health_failures += 1
            total_error_lines += obs.error_log_count
            if obs.memory_pct > max_mem_pct:
                max_mem_pct = obs.memory_pct
            notes.extend(obs.notes)
            # 다음 폴링까지 sleep — 단, 중단 가능한 sleep
            self._stop.wait(timeout=max(0.0, interval_s - (time.monotonic() - t_before)))

        elapsed = time.monotonic() - t_start
        error_log_rate_per_min = (total_error_lines / max(elapsed, 1.0)) * 60.0

        # 트리거 평가
        decision: AutoRollbackDecision = evaluate_triggers(
            self.contract.operational_policy.rollback_strategy,
            health_failure_count=health_failures,
            error_log_rate=error_log_rate_per_min,
            max_memory_pct=max_mem_pct,
        )

        # status 결정
        status = self._derive_status(
            decision=decision,
            health_failures=health_failures,
            error_log_rate=error_log_rate_per_min,
            threshold_per_min=threshold_per_min,
            max_mem_pct=max_mem_pct,
            interrupted=self._stop.is_set(),
        )

        if decision.triggered_by:
            notes.append("triggered: " + ", ".join(decision.triggered_by))

        return CVResult(
            deployment_id=self.deployment_id,
            duration_seconds=int(elapsed),
            health_failure_count=health_failures,
            error_log_rate=error_log_rate_per_min,
            max_memory_pct=max_mem_pct,
            status=status,
            notes=notes,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _derive_status(
        *,
        decision: AutoRollbackDecision,
        health_failures: int,
        error_log_rate: float,
        threshold_per_min: float,
        max_mem_pct: float,
        interrupted: bool,
    ) -> CVResultStatus:
        """관찰값 + decision 으로 최종 CVResultStatus 결정.

        규칙:
            - decision.should_rollback=True  → AUTO_ROLLBACK_PROPOSED
            - health 실패 OR rate 임계 초과 OR memory 80% 이상 → WARNING
            - 모두 정상 → STABLE
            - interrupted 시 STABLE 로 처리하되 notes 에 표시
        """
        if decision.should_rollback:
            return CVResultStatus.AUTO_ROLLBACK_PROPOSED

        warn = (
            health_failures > 0
            or error_log_rate > threshold_per_min
            or max_mem_pct >= 0.80
        )
        if warn:
            return CVResultStatus.WARNING
        return CVResultStatus.STABLE


# ---------------------------------------------------------------------------
# Null provider — 테스트 / docker 미설치 안전망
# ---------------------------------------------------------------------------


def _null_provider(_tick_duration: float) -> CVObservation:
    """모든 값 정상으로 가정. 외부 의존 없음."""
    return CVObservation(health_ok=True, error_log_count=0, memory_pct=0.0)


# ---------------------------------------------------------------------------
# Sync wrapper
# ---------------------------------------------------------------------------


def run_cv_sync(
    deployment_id: str,
    contract: ReleaseContract,
    *,
    metrics_provider: Optional[MetricsProvider] = None,
    max_duration_seconds: Optional[int] = None,
) -> CVResult:
    """편의 동기 진입점."""
    return CVMonitor(
        deployment_id,
        contract,
        metrics_provider,
        max_duration_seconds=max_duration_seconds,
    ).run()
