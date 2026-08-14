"""
recoder_watchdog.py — ReCoder EC2 Watchdog 데몬 (설계 §3.2.4 / §4.1.3).

EC2 인스턴스에서 24/7 실행되며 다음을 감지/보고한다:
  - 컨테이너 crash (이전 polling 에서 running → 현재 not running)
  - 헬스체크 N회 연속 실패 (기본 3회)
  - OOM kill (docker events stream)
  - 메모리 90% 초과 (docker stats)
  - 비정상 종료 후 restart (exit code != 0)

감지 시:
  1) incident.jsonl 에 append (설계 A.9 스키마)
  2) Discord webhook 으로 알림 전송 (요청 실패해도 jsonl 저장은 보장)
  3) 60초 spam suppression (동일 fingerprint 중복 알림 차단)

설계 제약:
  - 표준 라이브러리 + requests + psutil 만 사용. docker SDK 사용 금지.
  - core 모듈 import 금지 — 독립 배포 가능.
  - SIGTERM/SIGINT 수신 시 graceful shutdown.
  - 메모리 누수 없이 24/7 동작 (state dict 크기 상한, deque maxlen 적용).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover — install.sh 가 설치
    requests = None  # type: ignore

# psutil 은 선택적 — 미설치 시 시스템 메모리 측정 skip
try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore

# 같은 패키지 내 모듈은 두 가지 import 경로를 모두 지원 (스크립트/패키지)
try:
    from .config import WatchdogConfig, load_config
    from .docker_monitor import (
        ContainerInfo,
        ContainerStats,
        DockerEvent,
        DockerEventStream,
        DockerUnavailableError,
        docker_is_healthy,
        get_container_logs,
        get_container_stats,
        inspect_container,
        list_containers,
    )
    from .masking import MASK_VERSION, mask_lines, mask_text
    from .notifier import notify_discord
    from .cloudwatch_monitor import (
        CloudWatchUnavailableError, EcsTarget,
        collect_service_health, collect_traffic_metrics,
    )
    from .cloudwatch_thresholds import Thresholds, judge, next_unhealthy_streak
except ImportError:  # 스크립트로 직접 실행될 때
    # /opt/recoder/watchdog/recoder_watchdog.py 처럼 절대 경로 실행 케이스
    _here = Path(__file__).resolve().parent
    sys.path.insert(0, str(_here.parent))
    from watchdog.config import WatchdogConfig, load_config  # type: ignore
    from watchdog.docker_monitor import (  # type: ignore
        ContainerInfo,
        ContainerStats,
        DockerEvent,
        DockerEventStream,
        DockerUnavailableError,
        docker_is_healthy,
        get_container_logs,
        get_container_stats,
        inspect_container,
        list_containers,
    )
    from watchdog.masking import MASK_VERSION, mask_lines, mask_text  # type: ignore
    from watchdog.notifier import notify_discord  # type: ignore
    from watchdog.cloudwatch_monitor import (  # type: ignore
        CloudWatchUnavailableError, EcsTarget,
        collect_service_health, collect_traffic_metrics,
    )
    from watchdog.cloudwatch_thresholds import (  # type: ignore
        Thresholds, judge, next_unhealthy_streak,
    )


log = logging.getLogger("recoder.watchdog")


# ---------------------------------------------------------------------------
# 상태 데이터클래스
# ---------------------------------------------------------------------------


@dataclass
class ContainerState:
    name: str
    last_state: str = "unknown"            # "running" / "exited" / ...
    last_seen_running_at: float = 0.0
    last_status_change_at: float = 0.0
    last_exit_code: Optional[int] = None
    last_image: str = ""


@dataclass
class HealthState:
    name: str
    url: str
    consecutive_failures: int = 0
    last_status_code: Optional[int] = None
    last_error: Optional[str] = None
    last_checked_at: float = 0.0
    last_latency_ms: Optional[float] = None
    alert_active: bool = False


@dataclass
class FingerprintCache:
    """fingerprint → 마지막 알림 시각 (epoch). spam suppression 용.

    OrderedDict 로 LRU 관리 — 메모리 누수 방지.
    """
    window_seconds: float
    max_entries: int = 4096
    _store: "OrderedDict[str, float]" = field(default_factory=OrderedDict)

    def should_suppress(self, fingerprint: str, now: float) -> bool:
        # 만료된 엔트리 정리 (lazy)
        self._evict_expired(now)
        last = self._store.get(fingerprint)
        if last is not None and (now - last) < self.window_seconds:
            return True
        self._store[fingerprint] = now
        self._store.move_to_end(fingerprint)
        # 크기 상한
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)
        return False

    def _evict_expired(self, now: float) -> None:
        # 오래된 항목만 앞에서 제거
        cutoff = now - self.window_seconds
        while self._store:
            key, ts = next(iter(self._store.items()))
            if ts < cutoff:
                self._store.pop(key)
            else:
                break


# ---------------------------------------------------------------------------
# Watchdog 메인 클래스
# ---------------------------------------------------------------------------


class RecoderWatchdog:
    """EC2 Watchdog 데몬."""

    def __init__(self, cfg: WatchdogConfig) -> None:
        self.cfg = cfg
        self.shutdown_event = threading.Event()
        self.container_states: Dict[str, ContainerState] = {}
        self.health_states: Dict[str, HealthState] = {
            name: HealthState(name=name, url=url)
            for name, url in cfg.health_check_urls.items()
        }
        self.fingerprint_cache = FingerprintCache(window_seconds=cfg.spam_window_seconds)
        self._events_thread: Optional[threading.Thread] = None
        self._events_stream: Optional[DockerEventStream] = None
        self._last_health_run_at: float = 0.0
        self._docker_unavailable_since: Optional[float] = None
        #: ECS 가 연속으로 목표치를 못 채운 횟수. 정상으로 돌아오면 0 이 된다.
        self._ecs_unhealthy_streak: int = 0
        self._ecs_unavailable_since: Optional[float] = None
        # 최근 알림 정보 (디버그/테스트용)
        self._recent_alerts: Deque[Dict[str, Any]] = deque(maxlen=100)

    # -----------------------------------------------------------------
    # 시그널 / shutdown
    # -----------------------------------------------------------------

    def install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: Any) -> None:
            log.info("received signal %s, initiating graceful shutdown", signum)
            self.shutdown_event.set()

        for sig_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handler)
                except (ValueError, OSError):
                    # Windows 또는 비메인 thread 환경 — 무시
                    pass

    # -----------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------

    def run(self) -> int:
        log.info("recoder watchdog starting: %s", self.cfg.summary())
        self.install_signal_handlers()
        self._ensure_incident_path()

        # docker events stream 은 별도 thread 에서 처리
        self._start_events_thread()

        next_poll_at = 0.0
        next_health_at = 0.0
        next_ecs_at = 0.0
        try:
            while not self.shutdown_event.is_set():
                now = time.monotonic()
                if now >= next_poll_at:
                    self._safe_poll_containers()
                    next_poll_at = now + self.cfg.poll_interval_seconds
                if now >= next_health_at:
                    self._safe_health_checks()
                    next_health_at = now + self.cfg.health_interval_seconds
                if self.cfg.ecs_enabled and now >= next_ecs_at:
                    self._safe_poll_ecs()
                    next_ecs_at = now + self.cfg.ecs_interval_seconds
                # 짧게 sleep — shutdown 신호 빠른 반응
                self.shutdown_event.wait(timeout=min(1.0, self.cfg.poll_interval_seconds))
        except Exception as exc:  # noqa: BLE001 — 메인 루프 보호
            log.exception("watchdog main loop crashed: %s", exc)
            return 2
        finally:
            self._stop_events_thread()
            log.info("recoder watchdog stopped")
        return 0

    # -----------------------------------------------------------------
    # ECS / CloudWatch polling (FR-06-01/02)
    # -----------------------------------------------------------------

    def _safe_poll_ecs(self) -> None:
        """AWS 를 못 읽어도 데몬을 죽이지 않는다.

        다만 **조용히 넘어가지도 않는다.** 감시가 꺼진 것을 정상으로 두면,
        앱이 죽어도 아무 알림이 안 온다. 일정 시간 이상 계속 못 읽으면
        그 자체를 알린다(docker daemon 이 죽었을 때와 같은 규약).
        """
        try:
            self._poll_ecs()
            self._ecs_unavailable_since = None
        except CloudWatchUnavailableError as exc:
            now = time.time()
            if self._ecs_unavailable_since is None:
                self._ecs_unavailable_since = now
            log.warning("ECS 감시 실패: %s", exc)
            if now - self._ecs_unavailable_since >= 180:
                self._emit_alert(
                    alert_type="ecs_monitoring_unavailable",
                    severity="warning",
                    container_name=f"{self.cfg.ecs_cluster}/{self.cfg.ecs_service}",
                    message=(
                        f"3분 넘게 ECS 상태를 읽지 못했습니다 — 이 서비스는 지금 "
                        f"감시되지 않고 있습니다. AWS 자격증명과 권한을 확인하세요. ({exc})"
                    ),
                    logs_excerpt=[],
                    health_check_result={},
                    metric_snapshot={},
                )
                self._ecs_unavailable_since = now      # 재알림 간격 확보
        except Exception as exc:  # noqa: BLE001 — 감시 루프 보호
            # **예기치 못한 오류도 "감시 안 됨" 으로 센다.**
            #
            # 여기서 그냥 로그만 남기고 넘어가면, 원인이 무엇이든 계속 실패하는
            # 동안 사용자에게는 아무 알림이 안 간다. 앱이 죽어도 조용하다 —
            # 이 기능에서 가장 위험한 실패 방식이다. 그래서 위 분기와 같은
            # 타이머를 공유해, 오래 이어지면 반드시 드러나게 한다.
            #
            # (import 경로가 갈려 CloudWatchUnavailableError 의 클래스 객체가
            #  달라지는 경우처럼, 전용 분기를 비껴가는 상황이 실제로 있다.)
            log.exception("ECS 감시 중 예기치 못한 오류: %s", exc)
            now = time.time()
            if self._ecs_unavailable_since is None:
                self._ecs_unavailable_since = now
            if now - self._ecs_unavailable_since >= 180:
                self._emit_alert(
                    alert_type="ecs_monitoring_unavailable",
                    severity="warning",
                    container_name=f"{self.cfg.ecs_cluster}/{self.cfg.ecs_service}",
                    message=(
                        f"3분 넘게 ECS 상태를 읽지 못했습니다 — 이 서비스는 지금 "
                        f"감시되지 않고 있습니다. ({exc.__class__.__name__}: {exc})"
                    ),
                    logs_excerpt=[],
                    health_check_result={},
                    metric_snapshot={},
                )
                self._ecs_unavailable_since = now

    def _poll_ecs(self) -> None:
        target = EcsTarget(
            cluster=self.cfg.ecs_cluster,
            service=self.cfg.ecs_service,
            region=self.cfg.aws_region,
            load_balancer=self.cfg.alb_name,
            target_group=self.cfg.target_group,
        )
        window = self.cfg.ecs_window_seconds

        health = collect_service_health(target, window_seconds=window)
        metrics = collect_traffic_metrics(target, window_seconds=window)

        # 연속 미달 횟수는 **판정 전에** 갱신한다 — 이번 관측을 포함해서 센다.
        self._ecs_unhealthy_streak = next_unhealthy_streak(health, self._ecs_unhealthy_streak)

        thresholds = Thresholds(
            error_rate=self.cfg.error_rate_threshold,
            min_requests=self.cfg.min_requests,
            p95_seconds=self.cfg.p95_threshold_seconds,
            unhealthy_polls=self.cfg.unhealthy_polls,
        )
        anomalies = judge(health, metrics, thresholds, self._ecs_unhealthy_streak)

        snapshot = {
            "running": health.running,
            "desired": health.desired,
            "pending": health.pending,
            "stopped_recently": health.stopped_recently,
            "requests": metrics.requests,
            "errors_5xx": metrics.errors_5xx,
            "error_rate": metrics.error_rate,
            "p95_seconds": metrics.p95_seconds,
            "window_seconds": window,
        }
        log.debug("ECS 상태 %s", snapshot)

        for anomaly in anomalies:
            self._emit_alert(
                alert_type=anomaly.alert_type,
                severity=anomaly.severity,
                container_name=f"{self.cfg.ecs_cluster}/{self.cfg.ecs_service}",
                message=anomaly.message,
                logs_excerpt=[],
                health_check_result={},
                metric_snapshot={**snapshot, **anomaly.metrics},
            )

    # -----------------------------------------------------------------
    # Container polling
    # -----------------------------------------------------------------

    def _safe_poll_containers(self) -> None:
        try:
            self._poll_containers()
            self._docker_unavailable_since = None
        except DockerUnavailableError as exc:
            now = time.time()
            if self._docker_unavailable_since is None:
                self._docker_unavailable_since = now
                log.warning("docker unavailable: %s", exc)
                self._emit_alert(
                    alert_type="docker_daemon_unavailable",
                    severity="warning",
                    container_name="",
                    message=f"docker daemon not reachable: {exc}",
                    logs_excerpt=[],
                    health_check_result={},
                    metric_snapshot={},
                )
            # 첫 알림 이후엔 조용히 재시도
        except Exception as exc:  # noqa: BLE001
            log.exception("poll_containers error: %s", exc)

    def _poll_containers(self) -> None:
        containers = list_containers(include_all=True)
        # 메모리 정리: 사라진 컨테이너 상태 제거
        seen_names = set()
        now_wall = time.time()

        # stats 는 expensive — running 컨테이너만 대상으로 1회 샘플링
        stats_by_name: Dict[str, ContainerStats] = {}
        running_names = [c.name for c in containers if c.is_running and c.name]
        if running_names:
            try:
                for st in get_container_stats():
                    if st.name:
                        stats_by_name[st.name] = st
            except DockerUnavailableError:
                # stats 실패는 fatal 아님 — 컨테이너 상태 변화만 계속 본다
                pass

        for c in containers:
            if not c.name:
                continue
            seen_names.add(c.name)
            prev = self.container_states.get(c.name)
            current_state = "running" if c.is_running else (c.state.lower() or c.status.lower() or "stopped")
            if prev is None:
                prev = ContainerState(name=c.name)
                self.container_states[c.name] = prev
                prev.last_state = current_state
                prev.last_image = c.image
                if c.is_running:
                    prev.last_seen_running_at = now_wall

            # 상태 전이 감지
            if c.is_running:
                # crash 회복 후 다시 running 이 되었거나 그대로 running
                prev.last_seen_running_at = now_wall
                # 메모리 임계치 체크
                st = stats_by_name.get(c.name)
                if st is not None and st.mem_percent >= self.cfg.memory_threshold_percent:
                    self._emit_alert(
                        alert_type="container_memory_high",
                        severity="warning",
                        container_name=c.name,
                        message=(
                            f"memory usage {st.mem_percent:.1f}% >= threshold "
                            f"{self.cfg.memory_threshold_percent:.1f}%"
                        ),
                        logs_excerpt=self._safe_logs(c.name),
                        health_check_result={},
                        metric_snapshot={
                            "cpu_percent": st.cpu_percent,
                            "mem_percent": st.mem_percent,
                            "mem_usage": st.mem_usage,
                        },
                    )
                if prev.last_state != "running":
                    prev.last_status_change_at = now_wall
                prev.last_state = "running"
                prev.last_image = c.image
                continue

            # 여기 도달 = 비러닝 상태 (exited/restarting/dead)
            # 직전 polling 에서 running 이었으면 crash
            was_running = prev.last_state == "running" and prev.last_seen_running_at > 0
            if was_running:
                # docker inspect 로 exit code 확보 시도
                exit_code: Optional[int] = None
                try:
                    inspected = inspect_container(c.name)
                    state_obj = inspected.get("State") or {}
                    exit_code = state_obj.get("ExitCode")
                except DockerUnavailableError:
                    exit_code = None
                prev.last_exit_code = exit_code
                severity = "critical" if (exit_code not in (None, 0)) else "warning"
                self._emit_alert(
                    alert_type="container_crash",
                    severity=severity,
                    container_name=c.name,
                    message=(
                        f"container {c.name} transitioned running → {current_state} "
                        f"(exit_code={exit_code})"
                    ),
                    logs_excerpt=self._safe_logs(c.name),
                    health_check_result={},
                    metric_snapshot={"exit_code": exit_code, "image": c.image},
                )
            prev.last_state = current_state
            prev.last_status_change_at = now_wall

        # 사라진 컨테이너 (remove) 정리 — 메모리 누수 방지
        for stale in list(self.container_states.keys()):
            if stale not in seen_names:
                self.container_states.pop(stale, None)

    # -----------------------------------------------------------------
    # Health checks
    # -----------------------------------------------------------------

    def _safe_health_checks(self) -> None:
        try:
            self._run_health_checks()
        except Exception as exc:  # noqa: BLE001
            log.exception("health_checks error: %s", exc)

    def _run_health_checks(self) -> None:
        if not self.health_states:
            return
        if requests is None:
            log.debug("requests not installed — skipping health checks")
            return
        for state in self.health_states.values():
            start = time.monotonic()
            status_code: Optional[int] = None
            err: Optional[str] = None
            try:
                resp = requests.get(  # type: ignore[attr-defined]
                    state.url,
                    timeout=self.cfg.health_timeout_seconds,
                )
                status_code = resp.status_code
                ok = 200 <= resp.status_code < 300
            except Exception as exc:  # noqa: BLE001
                ok = False
                err = str(exc)[:300]
            latency_ms = (time.monotonic() - start) * 1000.0
            state.last_status_code = status_code
            state.last_error = err
            state.last_latency_ms = latency_ms
            state.last_checked_at = time.time()
            if ok:
                if state.consecutive_failures > 0 and state.alert_active:
                    # 회복 알림 (info)
                    self._emit_alert(
                        alert_type="health_check_recovered",
                        severity="info",
                        container_name=state.name,
                        message=f"health check {state.name} recovered ({state.url})",
                        logs_excerpt=[],
                        health_check_result={
                            "name": state.name,
                            "url": state.url,
                            "status_code": status_code,
                            "latency_ms": round(latency_ms, 1),
                        },
                        metric_snapshot={},
                    )
                state.consecutive_failures = 0
                state.alert_active = False
            else:
                state.consecutive_failures += 1
                if (
                    state.consecutive_failures >= self.cfg.health_fail_threshold
                    and not state.alert_active
                ):
                    state.alert_active = True
                    self._emit_alert(
                        alert_type="health_check_failed",
                        severity="critical",
                        container_name=state.name,
                        message=(
                            f"health check {state.name} failed "
                            f"{state.consecutive_failures} consecutive times"
                        ),
                        logs_excerpt=self._safe_logs(state.name),
                        health_check_result={
                            "name": state.name,
                            "url": state.url,
                            "status_code": status_code,
                            "error": err,
                            "latency_ms": round(latency_ms, 1),
                            "consecutive_failures": state.consecutive_failures,
                        },
                        metric_snapshot={},
                    )

    # -----------------------------------------------------------------
    # Docker events stream (oom / die)
    # -----------------------------------------------------------------

    def _start_events_thread(self) -> None:
        def _worker() -> None:
            while not self.shutdown_event.is_set():
                try:
                    with DockerEventStream(["event=oom", "event=die"]) as stream:
                        self._events_stream = stream
                        for ev in stream.iter_events():
                            if self.shutdown_event.is_set():
                                break
                            try:
                                self._handle_docker_event(ev)
                            except Exception as exc:  # noqa: BLE001
                                log.exception("event handler error: %s", exc)
                except DockerUnavailableError as exc:
                    log.warning("events stream unavailable: %s; retrying in 10s", exc)
                    self.shutdown_event.wait(timeout=10.0)
                except Exception as exc:  # noqa: BLE001
                    log.exception("events thread error: %s", exc)
                    self.shutdown_event.wait(timeout=10.0)
                finally:
                    self._events_stream = None

        self._events_thread = threading.Thread(
            target=_worker,
            name="watchdog-events",
            daemon=True,
        )
        self._events_thread.start()

    def _stop_events_thread(self) -> None:
        try:
            if self._events_stream is not None:
                self._events_stream.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("close events stream failed: %s", exc)
        if self._events_thread is not None:
            self._events_thread.join(timeout=5.0)
            self._events_thread = None

    def _handle_docker_event(self, ev: DockerEvent) -> None:
        if not ev.action:
            return
        name = ev.container_name or ev.container_id or "unknown"
        if ev.action == "oom":
            self._emit_alert(
                alert_type="container_oom_killed",
                severity="critical",
                container_name=name,
                message=f"OOM kill detected on container {name}",
                logs_excerpt=self._safe_logs(name),
                health_check_result={},
                metric_snapshot={"event": "oom"},
            )
        elif ev.action == "die":
            # exit_code != 0 인 경우만 알림 (정상 종료는 무시)
            if ev.exit_code not in (None, 0):
                self._emit_alert(
                    alert_type="container_exit_nonzero",
                    severity="warning",
                    container_name=name,
                    message=(
                        f"container {name} exited with code={ev.exit_code}"
                    ),
                    logs_excerpt=self._safe_logs(name),
                    health_check_result={},
                    metric_snapshot={"event": "die", "exit_code": ev.exit_code},
                )

    # -----------------------------------------------------------------
    # 알림 emit (incident.jsonl + Discord)
    # -----------------------------------------------------------------

    def _safe_logs(self, container_name: str) -> List[str]:
        if not container_name:
            return []
        try:
            blob = get_container_logs(container_name, since_seconds=60, tail=100)
        except DockerUnavailableError:
            return []
        except Exception as exc:  # noqa: BLE001
            log.debug("logs fetch failed for %s: %s", container_name, exc)
            return []
        lines = blob.splitlines() if blob else []
        return mask_lines(lines, max_lines=50, max_line_len=500)

    def _compute_fingerprint(self, alert_type: str, container_name: str, masked_message: str) -> str:
        # 메시지에서 숫자/타임스탬프를 잘라 fingerprint 가 폭주하지 않도록 prefix 만 사용
        msg_prefix = masked_message[:160] if masked_message else ""
        raw = f"{alert_type}|{container_name}|{msg_prefix}"
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()

    def _emit_alert(
        self,
        *,
        alert_type: str,
        severity: str,
        container_name: str,
        message: str,
        logs_excerpt: List[str],
        health_check_result: Dict[str, Any],
        metric_snapshot: Dict[str, Any],
    ) -> None:
        masked_message = mask_text(message)
        fingerprint = self._compute_fingerprint(alert_type, container_name, masked_message)
        now_epoch = time.time()
        if self.fingerprint_cache.should_suppress(fingerprint, now_epoch):
            log.debug(
                "suppress duplicate alert fingerprint=%s type=%s container=%s",
                fingerprint[:12], alert_type, container_name,
            )
            return

        # 시스템 메모리 (psutil 가능 시)
        sys_mem_percent: Optional[float] = None
        if psutil is not None:
            try:
                sys_mem_percent = float(psutil.virtual_memory().percent)
            except Exception:  # noqa: BLE001
                sys_mem_percent = None

        metric_snapshot = {**metric_snapshot}
        if sys_mem_percent is not None and "host_mem_percent" not in metric_snapshot:
            metric_snapshot["host_mem_percent"] = sys_mem_percent

        alert: Dict[str, Any] = {
            "alert_id": str(uuid.uuid4()),
            "source": "watchdog",
            "project_id": self.cfg.project_id,
            "environment": self.cfg.environment,
            "host": self.cfg.host,
            "container_name": container_name,
            "alert_type": alert_type,
            "severity": severity,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "message": masked_message,
            "logs_excerpt": logs_excerpt,  # already masked by _safe_logs
            "health_check_result": health_check_result,
            "metric_snapshot": metric_snapshot,
            "recent_deployment_id": self.cfg.deployment_id,
            "fingerprint": fingerprint,
            "mask_version": MASK_VERSION,
        }

        # 1) 파일 append (실패해도 Discord 시도)
        self._append_incident(alert)

        # 2) Discord 알림 (실패해도 jsonl 은 이미 저장됨)
        try:
            notify_discord(self.cfg.discord_webhook_url, alert)
        except Exception as exc:  # noqa: BLE001
            log.warning("discord notify dispatch failed: %s", exc)

        # 디버그 캐시
        self._recent_alerts.append(alert)

    def _ensure_incident_path(self) -> None:
        try:
            self.cfg.incident_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("failed to create incident dir %s: %s", self.cfg.incident_path.parent, exc)

    def _append_incident(self, alert: Dict[str, Any]) -> None:
        line = json.dumps(alert, ensure_ascii=False)
        try:
            with self.cfg.incident_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            log.info(
                "incident appended type=%s container=%s severity=%s alert_id=%s",
                alert.get("alert_type"),
                alert.get("container_name"),
                alert.get("severity"),
                alert.get("alert_id"),
            )
        except OSError as exc:
            log.error("failed to append incident.jsonl: %s", exc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="recoder-watchdog",
        description="ReCoder EC2 Watchdog daemon (incident detection + Discord alerts)",
    )
    p.add_argument(
        "--dotenv",
        type=str,
        default=None,
        help="Optional dotenv file to load before reading env vars",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration + docker availability then exit",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    dotenv_path = Path(args.dotenv) if args.dotenv else None
    cfg = load_config(dotenv_path=dotenv_path)
    _configure_logging(cfg.log_level)

    if args.check:
        log.info("config: %s", cfg.summary())
        ok = docker_is_healthy()
        log.info("docker daemon healthy: %s", ok)
        return 0 if ok else 1

    wd = RecoderWatchdog(cfg)
    return wd.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["RecoderWatchdog", "main"]
