"""
ReCoder — Continuous Verification (설계 §4.6 / §34).

배포 성공 직후 5분 동안 컨테이너를 자동 감시.

- 30초 간격 HTTP health check (httpx)
- 1분 간격 컨테이너 로그 수집 → 마스킹 후 ERROR/CRITICAL rate 계산
- 1분 간격 컨테이너 리소스 (메모리 / CPU) 사용량 추적

임계치:
    health 3회 연속 실패  → Auto Rollback 제안 (Approval Level 3)
    error log rate > 10/min → Warning + 롤백 옵션
    memory usage > 90%   → Warning

5분 완료:
    모두 정상     → DeploymentLedger.status = STABLE
    문제 발견     → IncidentMemory 에 패턴 저장 + DeploymentLedger.status 갱신

다른 모듈 (``cv/monitor.py``) 도 비슷한 일을 하지만, 본 모듈은:
  - asyncio.Task 백그라운드 실행 (FastAPI 이벤트 루프와 통합)
  - 30초/1분 주기 비대칭 폴링
  - 다중 verification 을 deployment_id 단위로 관리하는 싱글톤 매니저 제공
  - REST 엔드포인트 (``/api/deploy/verification/{deployment_id}/status``,
    ``/api/deploy/verification/{deployment_id}/stop``) 에서 직접 사용
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

try:
    import httpx  # type: ignore
except ImportError:  # pragma: no cover — optional dep
    httpx = None  # type: ignore


# ---------------------------------------------------------------------------
# Log masking — prefer canonical context_gate, fall back to local regex
# ---------------------------------------------------------------------------


def _fallback_mask(text: str) -> str:
    """간단한 정규식 마스킹. context_gate 가 없을 때만 사용."""
    if not text:
        return text
    patterns: list[tuple[re.Pattern[str], str]] = [
        (re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token|auth[_-]?token|private[_-]?key)\s*[=:]\s*\S+"),
         r"\1=<REDACTED>"),
        (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<REDACTED_AWS_KEY>"),
        (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "<REDACTED_GH_TOKEN>"),
        (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "<REDACTED_OPENAI_KEY>"),
        (re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "<REDACTED_JWT>"),
        (re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]{16,}"), "Bearer <REDACTED>"),
    ]
    out = text
    for pat, repl in patterns:
        out = pat.sub(repl, out)
    return out


def _mask(text: str) -> str:
    """가능하면 context_gate.mask_secrets 사용, 실패 시 fallback."""
    if not text:
        return text
    try:  # canonical project masking
        from context_gate import mask_secrets  # type: ignore
        return mask_secrets(text)
    except Exception:
        try:
            from core.context_gate import mask_secrets  # type: ignore
            return mask_secrets(text)
        except Exception:
            return _fallback_mask(text)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_DURATION_MINUTES = 5
HEALTH_CHECK_INTERVAL_SEC = 30
LOG_POLL_INTERVAL_SEC = 60
RESOURCE_POLL_INTERVAL_SEC = 60

HEALTH_FAILURE_THRESHOLD = 3       # 연속 실패 횟수 → auto rollback 제안
ERROR_LOG_RATE_THRESHOLD = 10.0    # per-minute
MEMORY_PCT_THRESHOLD = 90.0        # percent

_ERROR_LINE_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL)\b")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VerificationState:
    """단일 deployment 의 진행 중인 verification 상태."""

    def __init__(
        self,
        *,
        deployment_id: str,
        container_name: Optional[str],
        health_check_url: Optional[str],
        duration_minutes: int,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> None:
        self.deployment_id: str = deployment_id
        self.container_name: Optional[str] = container_name
        self.health_check_url: Optional[str] = health_check_url
        self.duration_minutes: int = duration_minutes
        self.session_id: Optional[str] = session_id
        self.project_id: Optional[str] = project_id

        self.started_at: str = _utcnow_iso()
        self.finished_at: Optional[str] = None
        self.status: str = "running"  # running | stable | unstable | stopped | error

        self.health_checks: list[dict] = []
        self.log_summary: list[dict] = []
        self.resource_summary: list[dict] = []
        self.anomalies: list[dict] = []

        # rolling counters
        self.consecutive_health_failures: int = 0
        self.max_memory_pct: float = 0.0
        self.max_cpu_pct: float = 0.0
        self.max_error_rate_per_min: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "deployment_id":   self.deployment_id,
            "container_name":  self.container_name,
            "health_check_url": self.health_check_url,
            "duration_minutes": self.duration_minutes,
            "session_id":      self.session_id,
            "project_id":      self.project_id,
            "started_at":      self.started_at,
            "finished_at":     self.finished_at,
            "status":          self.status,
            "health_checks":   list(self.health_checks),
            "log_summary":     list(self.log_summary),
            "resource_summary": list(self.resource_summary),
            "anomalies":       list(self.anomalies),
            "counters": {
                "consecutive_health_failures": self.consecutive_health_failures,
                "max_memory_pct":              self.max_memory_pct,
                "max_cpu_pct":                 self.max_cpu_pct,
                "max_error_rate_per_min":      self.max_error_rate_per_min,
            },
        }


# Callback signature: invoked when a threshold is exceeded.
#   await callback(deployment_id, anomaly_dict)
ThresholdCallback = Callable[[str, dict], Awaitable[None]]


# ---------------------------------------------------------------------------
# Helpers — docker stats / docker logs parsing
# ---------------------------------------------------------------------------


def _run_subprocess(cmd: list[str], timeout: float = 10.0) -> Optional[subprocess.CompletedProcess]:
    """Docker subprocess 호출. 실패 시 silent None (container 가 죽었거나 docker 미설치)."""
    try:
        return subprocess.run(  # noqa: S603 — controlled args
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        log.debug("docker subprocess failed (%s): %s", cmd[:3], exc)
        return None


def _docker_logs_since(container: str, seconds: int) -> str:
    """``docker logs --since {N}s <container>`` → stdout+stderr (마스킹 전)."""
    if not container:
        return ""
    res = _run_subprocess(
        ["docker", "logs", "--since", f"{seconds}s", container],
        timeout=15.0,
    )
    if res is None:
        return ""
    # docker logs 는 stdout/stderr 둘 다 사용 — 두 스트림 합치기.
    return (res.stdout or "") + (res.stderr or "")


def _docker_stats(container: str) -> Optional[dict]:
    """``docker stats --no-stream --format json <container>`` 결과 dict 1개 반환.

    실패 시 None. 일부 docker 버전은 --format json 미지원 → ``{{json .}}`` fallback.
    """
    if not container:
        return None
    candidates = [
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", container],
        ["docker", "stats", "--no-stream", "--format", "json", container],
    ]
    for cmd in candidates:
        res = _run_subprocess(cmd, timeout=10.0)
        if res is None or res.returncode != 0:
            continue
        line = (res.stdout or "").strip().splitlines()
        if not line:
            continue
        try:
            return json.loads(line[0])
        except json.JSONDecodeError:
            continue
    return None


def _parse_percent(value: Any) -> float:
    """``"42.5%"`` / ``42.5`` / None → 42.5 / 0.0."""
    if value is None:
        return 0.0
    s = str(value).strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _count_error_lines(masked_text: str) -> int:
    if not masked_text:
        return 0
    n = 0
    for line in masked_text.splitlines():
        if _ERROR_LINE_RE.search(line):
            n += 1
    return n


# ---------------------------------------------------------------------------
# Result persistence (JSONL + DeploymentLedger)
# ---------------------------------------------------------------------------


def _session_jsonl_path(session_id: Optional[str], deployment_id: str) -> Optional[Path]:
    """JSONL 결과 파일 경로. session_id 없으면 default 디렉토리."""
    base = Path.home() / ".recoder" / "sessions"
    if session_id:
        target_dir = base / session_id
    else:
        target_dir = base / "_unknown"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Cannot create CV log dir %s: %s", target_dir, exc)
        return None
    return target_dir / f"continuous_verification_{deployment_id}.jsonl"


def _append_jsonl(path: Optional[Path], entry: dict) -> None:
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        log.debug("JSONL append failed (%s): %s", path, exc)


def _try_update_ledger(state: VerificationState) -> None:
    """5분 완료 시 DeploymentLedger.status 업데이트 (best-effort)."""
    try:
        try:
            from persistence import RecoderDB, get_default_db_path, update_deployment_status  # type: ignore
            from schemas import DeploymentLedgerStatus  # type: ignore
        except ImportError:
            from core.persistence import RecoderDB, get_default_db_path, update_deployment_status  # type: ignore
            from core.schemas import DeploymentLedgerStatus  # type: ignore

        new_status = (
            DeploymentLedgerStatus.STABLE
            if state.status == "stable"
            else DeploymentLedgerStatus.FAILED
        )
        db = RecoderDB(get_default_db_path())
        update_deployment_status(
            db,
            state.deployment_id,
            new_status,
            health_after="healthy" if state.status == "stable" else "unhealthy",
            failure_reason=(
                None
                if state.status == "stable"
                else ",".join(a.get("kind", "anomaly") for a in state.anomalies[:5])[:500]
            ),
        )
    except Exception as exc:  # noqa: BLE001 — best effort
        log.debug("DeploymentLedger update skipped: %s", exc)


def _try_save_incident(state: VerificationState) -> None:
    """문제 발견 시 IncidentMemory 에 패턴 저장 (best-effort)."""
    if state.status == "stable" or not state.anomalies:
        return
    try:
        try:
            from incident_memory.memory_store import init_incident_memory_table, save_incident_memory  # type: ignore
            from persistence import RecoderDB, get_default_db_path  # type: ignore
            from schemas import IncidentMemoryRecord  # type: ignore
        except ImportError:
            from core.incident_memory.memory_store import init_incident_memory_table, save_incident_memory  # type: ignore
            from core.persistence import RecoderDB, get_default_db_path  # type: ignore
            from core.schemas import IncidentMemoryRecord  # type: ignore

        symptom = "; ".join(a.get("message", a.get("kind", "")) for a in state.anomalies[:3])[:500]
        kinds = sorted({a.get("kind", "unknown") for a in state.anomalies})
        import hashlib
        fp_seed = f"cv:{state.project_id or 'unknown'}:{':'.join(kinds)}:{state.container_name or ''}"
        fingerprint = hashlib.sha256(fp_seed.encode("utf-8")).hexdigest()[:32]
        record = IncidentMemoryRecord(
            fingerprint=fingerprint,
            project_id=state.project_id,
            symptom=_mask(symptom) or "continuous verification anomaly",
            root_cause=f"5-min post-deploy verification flagged: {', '.join(kinds)}",
            successful_fix="pending — operator action required",
            applied_proposal_id=f"cv:{state.deployment_id}",
            linked_deployment_id=state.deployment_id,
            user_consent=False,
        )
        db = RecoderDB(get_default_db_path())
        init_incident_memory_table(db)
        save_incident_memory(db, record)
    except Exception as exc:  # noqa: BLE001
        log.debug("IncidentMemory save skipped: %s", exc)


# ---------------------------------------------------------------------------
# ContinuousVerifier
# ---------------------------------------------------------------------------


class ContinuousVerifier:
    """배포 후 5분 모니터링 — 다중 deployment 동시 관리."""

    def __init__(self) -> None:
        self._active_verifications: dict[str, asyncio.Task] = {}
        self._states: dict[str, VerificationState] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(
        self,
        deployment_id: str,
        container_name: Optional[str] = None,
        health_check_url: Optional[str] = None,
        duration_minutes: int = DEFAULT_DURATION_MINUTES,
        *,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        on_threshold_exceeded: Optional[ThresholdCallback] = None,
    ) -> dict[str, Any]:
        """백그라운드 verification 시작. 즉시 반환 (모니터링은 async task).

        Returns:
            ``{"deployment_id", "status": "started", "duration_minutes", ...}``
        """
        async with self._lock:
            # 이미 같은 deployment 가 있으면 기존을 깨끗이 cancel
            existing = self._active_verifications.get(deployment_id)
            if existing is not None and not existing.done():
                existing.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await existing

            state = VerificationState(
                deployment_id=deployment_id,
                container_name=container_name,
                health_check_url=health_check_url,
                duration_minutes=max(1, int(duration_minutes)),
                session_id=session_id,
                project_id=project_id,
            )
            self._states[deployment_id] = state

            task = asyncio.create_task(
                self._run(state, on_threshold_exceeded),
                name=f"cv-{deployment_id}",
            )
            self._active_verifications[deployment_id] = task

        return {
            "deployment_id":    deployment_id,
            "status":           "started",
            "duration_minutes": state.duration_minutes,
            "started_at":       state.started_at,
        }

    async def stop(self, deployment_id: str) -> dict[str, Any]:
        """강제 중지. Task 를 cancel 하고 현재까지 결과 반환."""
        async with self._lock:
            task = self._active_verifications.get(deployment_id)
            state = self._states.get(deployment_id)
        if task is None or state is None:
            return {"deployment_id": deployment_id, "status": "not_found"}

        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # state.status 가 running 이었으면 stopped 로 표시
        if state.status == "running":
            state.status = "stopped"
            state.finished_at = _utcnow_iso()

        return {"deployment_id": deployment_id, "status": "stopped", "result": state.snapshot()}

    def get_status(self, deployment_id: str) -> Optional[dict[str, Any]]:
        """진행 중이거나 완료된 verification 의 현재 스냅샷."""
        state = self._states.get(deployment_id)
        if state is None:
            return None
        return state.snapshot()

    def list_active(self) -> list[str]:
        return [
            dep_id
            for dep_id, t in self._active_verifications.items()
            if not t.done()
        ]

    # ------------------------------------------------------------------
    # Internal — run loop
    # ------------------------------------------------------------------

    async def _run(
        self,
        state: VerificationState,
        on_threshold_exceeded: Optional[ThresholdCallback],
    ) -> dict[str, Any]:
        """본 5분 모니터링 루프. 예외는 모두 catch — 배포 성공 처리에 영향 X."""
        jsonl_path = _session_jsonl_path(state.session_id, state.deployment_id)
        _append_jsonl(jsonl_path, {
            "event":         "start",
            "ts":            state.started_at,
            "deployment_id": state.deployment_id,
            "container":     state.container_name,
            "url":           state.health_check_url,
            "duration_min":  state.duration_minutes,
        })

        deadline = time.monotonic() + state.duration_minutes * 60.0
        next_health = time.monotonic()                            # 즉시 1회
        next_log    = time.monotonic() + LOG_POLL_INTERVAL_SEC
        next_stats  = time.monotonic() + RESOURCE_POLL_INTERVAL_SEC

        try:
            while True:
                now = time.monotonic()
                if now >= deadline:
                    break

                # 가장 가까운 다음 이벤트까지 sleep
                next_tick = min(next_health, next_log, next_stats, deadline)
                sleep_for = max(0.0, next_tick - now)
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)

                now = time.monotonic()
                if now >= deadline:
                    break

                if now >= next_health:
                    await self._tick_health(state, on_threshold_exceeded, jsonl_path)
                    next_health = now + HEALTH_CHECK_INTERVAL_SEC

                if now >= next_log:
                    await self._tick_logs(state, on_threshold_exceeded, jsonl_path)
                    next_log = now + LOG_POLL_INTERVAL_SEC

                if now >= next_stats:
                    await self._tick_stats(state, on_threshold_exceeded, jsonl_path)
                    next_stats = now + RESOURCE_POLL_INTERVAL_SEC

            # ── 5분 정상 완료 ───────────────────────────────────────
            state.status = "stable" if not state.anomalies else "unstable"
            state.finished_at = _utcnow_iso()

        except asyncio.CancelledError:
            # stop() 또는 서버 종료
            if state.status == "running":
                state.status = "stopped"
                state.finished_at = _utcnow_iso()
            _append_jsonl(jsonl_path, {
                "event":         "cancelled",
                "ts":            state.finished_at,
                "deployment_id": state.deployment_id,
            })
            raise

        except Exception as exc:  # noqa: BLE001 — verification 실패가 배포를 망가뜨리면 안 됨
            log.exception("Continuous verification failed: %s", exc)
            state.status = "error"
            state.finished_at = _utcnow_iso()
            state.anomalies.append({
                "kind":    "internal_error",
                "message": _mask(str(exc))[:500],
                "ts":      state.finished_at,
            })

        finally:
            _append_jsonl(jsonl_path, {
                "event":     "finish",
                "ts":        state.finished_at or _utcnow_iso(),
                "status":    state.status,
                "anomalies": state.anomalies,
                "counters": {
                    "consecutive_health_failures": state.consecutive_health_failures,
                    "max_memory_pct":              state.max_memory_pct,
                    "max_cpu_pct":                 state.max_cpu_pct,
                    "max_error_rate_per_min":      state.max_error_rate_per_min,
                },
            })
            # task 자기 자신을 active 목록에서 제거 (cancellation 처리 후)
            async with self._lock:
                t = self._active_verifications.get(state.deployment_id)
                if t is asyncio.current_task():
                    self._active_verifications.pop(state.deployment_id, None)

            # ledger / incident memory 갱신 — best effort
            if state.status in ("stable", "unstable"):
                _try_update_ledger(state)
                _try_save_incident(state)

        return state.snapshot()

    # ------------------------------------------------------------------
    # Tickers
    # ------------------------------------------------------------------

    async def _tick_health(
        self,
        state: VerificationState,
        on_threshold_exceeded: Optional[ThresholdCallback],
        jsonl_path: Optional[Path],
    ) -> None:
        """30초 간격 HTTP health check."""
        url = state.health_check_url
        ts = _utcnow_iso()
        record: dict[str, Any] = {"ts": ts, "url": url}

        if not url or httpx is None:
            record["status"] = "skipped"
            record["ok"] = True
            state.health_checks.append(record)
            _append_jsonl(jsonl_path, {"event": "health", **record})
            return

        ok = False
        status_code: Optional[int] = None
        err: Optional[str] = None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                status_code = resp.status_code
                ok = 200 <= status_code < 400
        except Exception as exc:  # noqa: BLE001
            err = _mask(str(exc))[:200]

        record["status_code"] = status_code
        record["ok"] = ok
        if err:
            record["error"] = err

        state.health_checks.append(record)
        _append_jsonl(jsonl_path, {"event": "health", **record})

        if ok:
            state.consecutive_health_failures = 0
        else:
            state.consecutive_health_failures += 1
            if state.consecutive_health_failures >= HEALTH_FAILURE_THRESHOLD:
                anomaly = {
                    "kind":            "health_failure",
                    "message":         f"{state.consecutive_health_failures} consecutive health failures",
                    "approval_level":  3,
                    "action":          "auto_rollback_proposed",
                    "ts":              ts,
                }
                state.anomalies.append(anomaly)
                _append_jsonl(jsonl_path, {"event": "anomaly", **anomaly})
                await self._notify(on_threshold_exceeded, state.deployment_id, anomaly)

    async def _tick_logs(
        self,
        state: VerificationState,
        on_threshold_exceeded: Optional[ThresholdCallback],
        jsonl_path: Optional[Path],
    ) -> None:
        """1분 간격 docker logs → ERROR/CRITICAL rate."""
        container = state.container_name
        ts = _utcnow_iso()

        if not container:
            record = {"ts": ts, "skipped": True}
            state.log_summary.append(record)
            _append_jsonl(jsonl_path, {"event": "logs", **record})
            return

        raw_logs = await asyncio.to_thread(
            _docker_logs_since, container, LOG_POLL_INTERVAL_SEC,
        )
        masked = _mask(raw_logs)
        err_count = _count_error_lines(masked)
        rate_per_min = float(err_count)  # window 가 60s 이므로 분당 비율
        state.max_error_rate_per_min = max(state.max_error_rate_per_min, rate_per_min)

        record = {
            "ts":               ts,
            "lines_total":      len(masked.splitlines()) if masked else 0,
            "error_count":      err_count,
            "rate_per_min":     rate_per_min,
        }
        state.log_summary.append(record)
        _append_jsonl(jsonl_path, {"event": "logs", **record})

        if rate_per_min > ERROR_LOG_RATE_THRESHOLD:
            anomaly = {
                "kind":             "error_log_rate",
                "message":          f"ERROR/CRITICAL rate {rate_per_min:.1f}/min > {ERROR_LOG_RATE_THRESHOLD}",
                "rate_per_min":     rate_per_min,
                "approval_level":   2,
                "action":           "warning_and_rollback_option",
                "ts":               ts,
            }
            state.anomalies.append(anomaly)
            _append_jsonl(jsonl_path, {"event": "anomaly", **anomaly})
            await self._notify(on_threshold_exceeded, state.deployment_id, anomaly)

    async def _tick_stats(
        self,
        state: VerificationState,
        on_threshold_exceeded: Optional[ThresholdCallback],
        jsonl_path: Optional[Path],
    ) -> None:
        """1분 간격 docker stats → 메모리 / CPU."""
        container = state.container_name
        ts = _utcnow_iso()

        if not container:
            record = {"ts": ts, "skipped": True}
            state.resource_summary.append(record)
            _append_jsonl(jsonl_path, {"event": "stats", **record})
            return

        stats = await asyncio.to_thread(_docker_stats, container)
        if not stats:
            record = {"ts": ts, "available": False}
            state.resource_summary.append(record)
            _append_jsonl(jsonl_path, {"event": "stats", **record})
            return

        mem_pct = _parse_percent(stats.get("MemPerc"))
        cpu_pct = _parse_percent(stats.get("CPUPerc"))
        state.max_memory_pct = max(state.max_memory_pct, mem_pct)
        state.max_cpu_pct = max(state.max_cpu_pct, cpu_pct)

        record = {
            "ts":          ts,
            "memory_pct":  mem_pct,
            "cpu_pct":     cpu_pct,
            "mem_usage":   stats.get("MemUsage"),
            "net_io":      stats.get("NetIO"),
            "block_io":    stats.get("BlockIO"),
        }
        state.resource_summary.append(record)
        _append_jsonl(jsonl_path, {"event": "stats", **record})

        if mem_pct > MEMORY_PCT_THRESHOLD:
            anomaly = {
                "kind":            "memory_high",
                "message":         f"memory usage {mem_pct:.1f}% > {MEMORY_PCT_THRESHOLD}%",
                "memory_pct":      mem_pct,
                "approval_level":  1,
                "action":          "warning",
                "ts":              ts,
            }
            state.anomalies.append(anomaly)
            _append_jsonl(jsonl_path, {"event": "anomaly", **anomaly})
            await self._notify(on_threshold_exceeded, state.deployment_id, anomaly)

    # ------------------------------------------------------------------
    # Callback dispatch — never let user callback break the loop
    # ------------------------------------------------------------------

    @staticmethod
    async def _notify(
        cb: Optional[ThresholdCallback],
        deployment_id: str,
        anomaly: dict,
    ) -> None:
        if cb is None:
            return
        try:
            await cb(deployment_id, anomaly)
        except Exception as exc:  # noqa: BLE001
            log.warning("on_threshold_exceeded callback failed: %s", exc)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


_verifier_singleton: Optional[ContinuousVerifier] = None


def get_continuous_verifier() -> ContinuousVerifier:
    """프로세스 단위 싱글톤. 여러 deployment 를 동시에 추적."""
    global _verifier_singleton
    if _verifier_singleton is None:
        _verifier_singleton = ContinuousVerifier()
    return _verifier_singleton


__all__ = [
    "ContinuousVerifier",
    "VerificationState",
    "get_continuous_verifier",
    "DEFAULT_DURATION_MINUTES",
    "HEALTH_CHECK_INTERVAL_SEC",
    "LOG_POLL_INTERVAL_SEC",
    "RESOURCE_POLL_INTERVAL_SEC",
    "HEALTH_FAILURE_THRESHOLD",
    "ERROR_LOG_RATE_THRESHOLD",
    "MEMORY_PCT_THRESHOLD",
]
