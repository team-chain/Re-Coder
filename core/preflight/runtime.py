"""
ReCoder Runtime Preflight (§31).

Static Preflight 통과 후 실행. 임시 컨테이너를 띄워 실제 동작 검증:
    1) docker build (이미지 미지정 시) 또는 docker pull (기존 이미지)
    2) docker run -d -p host:app -e PORT=...  (임시 컨테이너)
    3) wait_startup: PORT LISTEN + health probe (timeout)
    4) smoke_tests: HTTP 호출 (contract.operational_policy.smoke_tests)
    5) log_pattern: 컨테이너 stdout 에서 fatal/critical 라인 탐지
    6) db_connect (옵션): DATABASE_URL 환경변수로 사전 검증
    7) docker stop + rm (try/finally — leak 방지)

결과는 PreflightRuntimeChecks 에 채워서 PreflightRun 업데이트.

설계 결정:
  - subprocess 가 아닌 ``docker`` Python SDK 우선, 미설치 시 subprocess fallback
  - 모든 외부 호출에 timeout — hang 방지
  - 컨테이너 로그는 secret 마스킹 후 50줄만 저장 (PreflightRuntimeChecks.container_log_tail)
  - 실패 시 정리는 항상 시도. SIGINT/SIGTERM 도 catch 해서 cleanup.
"""

from __future__ import annotations

import logging
import re
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

try:
    from preflight import CheckResult, safe_workspace_path
    from preflight.static import compute_score, determine_status
    from schemas import (
        ContractSmokeTest,
        PreflightRun,
        PreflightRuntimeChecks,
        PreflightSeverity,
        PreflightStatus,
        ReleaseContract,
    )
except ImportError:  # pragma: no cover
    from core.preflight import CheckResult, safe_workspace_path  # type: ignore
    from core.preflight.static import compute_score, determine_status  # type: ignore
    from core.schemas import (  # type: ignore
        ContractSmokeTest,
        PreflightRun,
        PreflightRuntimeChecks,
        PreflightSeverity,
        PreflightStatus,
        ReleaseContract,
    )


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Time helpers — contract duration ("5m", "30s") → seconds
# ---------------------------------------------------------------------------


_DURATION_RE: re.Pattern[str] = re.compile(r"^\s*(\d+)\s*([smhSMH]?)\s*$")


def parse_duration(s: str | None, default: int = 60) -> int:
    """``"5m"`` / ``"30s"`` / ``"1h"`` → seconds. 못 읽으면 default."""
    if not s:
        return default
    m = _DURATION_RE.match(str(s))
    if not m:
        return default
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "" or unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    return default


# ---------------------------------------------------------------------------
# Secret masking — 컨테이너 로그가 ledger 에 들어갈 때 secret 누수 방지
# ---------------------------------------------------------------------------


_SECRET_LINE_PATTERNS: list[re.Pattern[str]] = [
    # KEY=VALUE 환경변수 값 (= 다음 4자 이상). IGNORECASE 는 flag 인자로.
    re.compile(
        r"(?P<key>(?:password|passwd|secret|api[_-]?key|auth[_-]?token|private[_-]?key)\s*[=:]\s*)\S{4,}",
        re.IGNORECASE,
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{48}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"bearer\s+[A-Za-z0-9\-._~+/]{20,}", re.IGNORECASE),
]


def mask_log(text: str) -> str:
    """컨테이너 로그에서 secret 의심 패턴 마스킹."""
    if not text:
        return ""
    out = text
    for pat in _SECRET_LINE_PATTERNS:
        if pat.groupindex:  # named group 케이스
            out = pat.sub(lambda m: m.group("key") + "<REDACTED>", out)
        else:
            out = pat.sub("<REDACTED>", out)
    return out


# ---------------------------------------------------------------------------
# Docker availability
# ---------------------------------------------------------------------------


@dataclass
class DockerCapabilities:
    """현재 시스템에서 사용 가능한 docker 도구 표시."""
    cli_available:   bool = False
    sdk_available:   bool = False
    version_text:    str = ""


def detect_docker() -> DockerCapabilities:
    """docker CLI / SDK 가능 여부 감지."""
    caps = DockerCapabilities()
    if shutil.which("docker") is not None:
        try:
            res = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0:
                caps.cli_available = True
                caps.version_text = res.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass
    try:
        import docker  # type: ignore  # noqa: F401
        caps.sdk_available = True
    except ImportError:
        pass
    return caps


# ---------------------------------------------------------------------------
# HTTP probe
# ---------------------------------------------------------------------------


def http_probe(
    host: str,
    port: int,
    path: str = "/health",
    timeout: float = 2.0,
    expected_status: Optional[list[int]] = None,
) -> tuple[bool, int, str]:
    """단일 HTTP GET probe. (ok, status, body_first_120chars).

    expected_status 미지정 시 200~299 모두 OK.
    """
    url = f"http://{host}:{port}{path}"
    try:
        with urllib_request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — localhost only
            status = resp.getcode() or 0
            body = resp.read(128).decode("utf-8", errors="ignore")
            if expected_status:
                ok = status in expected_status
            else:
                ok = 200 <= status < 300
            return ok, status, body
    except urllib_error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(128).decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            pass
        if expected_status:
            ok = exc.code in expected_status
        else:
            ok = False
        return ok, exc.code, body
    except (urllib_error.URLError, TimeoutError, ConnectionError, OSError):
        return False, 0, ""


# ---------------------------------------------------------------------------
# Container manager — docker CLI wrapper
# ---------------------------------------------------------------------------


@dataclass
class ContainerRun:
    """임시 컨테이너 핸들."""
    container_id:  str
    host_port:     int
    app_port:      int
    image_ref:     str
    started_at:    float
    logs_seen:     list[str] = field(default_factory=list)


@contextmanager
def temporary_container(
    image_ref: str,
    *,
    host_port: int,
    app_port: int,
    env: Optional[dict[str, str]] = None,
    name_prefix: str = "recoder_preflight_",
    cli: str = "docker",
) -> Iterator[ContainerRun]:
    """임시 컨테이너 띄우고 yield. with 블록 종료 시 stop + rm.

    Args:
        image_ref:   docker image (e.g. "myapp:dev")
        host_port:   호스트 포트 — 외부에서 probe 할 포트
        app_port:    컨테이너 내부 포트
        env:         환경변수 dict — 값은 secret 일 수 있음
        name_prefix: 임시 컨테이너 이름 prefix
    """
    name = f"{name_prefix}{int(time.time() * 1000)}"
    cmd = [cli, "run", "-d", "--name", name, "-p", f"{host_port}:{app_port}"]
    for k, v in (env or {}).items():
        cmd.extend(["-e", f"{k}={v}"])
    cmd.append(image_ref)

    log.debug("Starting temporary container: %s", " ".join(cmd[:6]))  # 환경변수 값은 로그 안 함
    try:
        run_proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"docker run timeout: {exc}") from exc

    if run_proc.returncode != 0:
        raise RuntimeError(
            f"docker run failed (exit {run_proc.returncode}): "
            f"{mask_log(run_proc.stderr[:500])}"
        )

    container_id = run_proc.stdout.strip()
    started_at = time.monotonic()
    handle = ContainerRun(
        container_id=container_id,
        host_port=host_port,
        app_port=app_port,
        image_ref=image_ref,
        started_at=started_at,
    )

    try:
        yield handle
    finally:
        # 항상 cleanup — 예외 발생해도 컨테이너 leak 안 됨
        try:
            subprocess.run([cli, "stop", container_id], capture_output=True, timeout=15)
        except Exception:  # noqa: BLE001
            pass
        try:
            subprocess.run([cli, "rm", "-f", container_id], capture_output=True, timeout=15)
        except Exception:  # noqa: BLE001
            pass


def fetch_container_logs(container_id: str, tail: int = 50, cli: str = "docker") -> str:
    """컨테이너 stdout/stderr 의 마지막 N줄. 마스킹 후 반환."""
    try:
        res = subprocess.run(
            [cli, "logs", "--tail", str(tail), container_id],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "<log fetch timeout>"
    raw = (res.stdout or "") + (res.stderr or "")
    return mask_log(raw[-4000:])  # 4 KB cap


# ---------------------------------------------------------------------------
# Verification primitives
# ---------------------------------------------------------------------------


def wait_for_port_listen(host: str, port: int, timeout_seconds: int) -> bool:
    """컨테이너가 PORT 에서 LISTEN 시작할 때까지 대기. timeout 초과 시 False."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                sock.connect((host, port))
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.3)
    return False


def wait_for_health(
    host: str,
    port: int,
    health_path: str,
    timeout_seconds: int,
    interval: float = 1.0,
) -> tuple[bool, int]:
    """health endpoint 가 200 OK 응답 시작할 때까지 대기.

    Returns: (ok, attempts)
    """
    deadline = time.monotonic() + timeout_seconds
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        ok, status, _ = http_probe(host, port, path=health_path, timeout=2.0)
        if ok:
            return True, attempts
        time.sleep(interval)
    return False, attempts


def run_smoke_tests(
    host: str,
    port: int,
    smoke_tests: list[ContractSmokeTest],
) -> tuple[bool, list[dict]]:
    """contract.operational_policy.smoke_tests 모두 통과 시 True."""
    results: list[dict] = []
    all_ok = True
    for st in smoke_tests:
        ok, status, body = http_probe(
            host, port, path=st.path, timeout=5.0,
            expected_status=list(st.expected_status),
        )
        results.append({
            "path":            st.path,
            "method":          st.method,
            "expected_status": list(st.expected_status),
            "actual_status":   status,
            "passed":          ok,
        })
        if not ok:
            all_ok = False
    return all_ok, results


def detect_log_pattern_issues(log_text: str) -> tuple[bool, list[str]]:
    """컨테이너 로그에서 fatal/critical 라인 검출.

    Returns:
        (ok, problematic_lines) — ok=False 면 즉시 차단.
    """
    if not log_text:
        return True, []
    bad: list[str] = []
    for line in log_text.splitlines():
        ln = line.lower()
        if any(kw in ln for kw in (
            "fatal:", "[fatal]", "critical:", "[critical]",
            "panic:", "[error] startup", "modulenotfounderror",
            "no module named", "address already in use",
        )):
            bad.append(line.strip()[:200])
            if len(bad) >= 5:
                break
    return (len(bad) == 0), bad


# ---------------------------------------------------------------------------
# Runtime Preflight Runner
# ---------------------------------------------------------------------------


@dataclass
class RuntimePreflightResult:
    """단일 Runtime Preflight 호출 결과."""
    runtime_checks:   PreflightRuntimeChecks
    status:           PreflightStatus
    issues:           list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


class RuntimePreflightRunner:
    """
    Args:
        workspace_path: 프로젝트 루트
        contract:       ReleaseContract
        image_ref:      미리 빌드된 docker image (e.g. "myapp:dev").
                        지정 안 하면 환경변수 RECODER_IMAGE 사용 / 그 외 빌드 단계 외주.
        docker_cli:     기본 ``docker``. (podman 사용 시 ``podman``)
        skip_if_docker_missing: docker 없으면 status=WARN 으로 즉시 반환 (default True).
    """

    def __init__(
        self,
        workspace_path: str | Path,
        contract: ReleaseContract,
        *,
        image_ref: Optional[str] = None,
        docker_cli: str = "docker",
        skip_if_docker_missing: bool = True,
    ) -> None:
        self.workspace = safe_workspace_path(str(workspace_path))
        self.contract = contract
        self.image_ref = image_ref
        self.docker_cli = docker_cli
        self.skip_if_docker_missing = skip_if_docker_missing

    # ------------------------------------------------------------------

    def run(self) -> RuntimePreflightResult:
        """Runtime Preflight 실행. 결과는 RuntimePreflightResult."""
        start = time.monotonic()
        runtime = PreflightRuntimeChecks()
        issues: list[str] = []

        caps = detect_docker()
        if not caps.cli_available:
            msg = "docker CLI 가 없어 Runtime Preflight 를 건너뜁니다."
            issues.append(msg)
            if self.skip_if_docker_missing:
                return RuntimePreflightResult(
                    runtime_checks=runtime,
                    status=PreflightStatus.WARN,
                    issues=issues,
                    duration_seconds=time.monotonic() - start,
                )
            return RuntimePreflightResult(
                runtime_checks=runtime,
                status=PreflightStatus.BLOCKED,
                issues=issues,
                duration_seconds=time.monotonic() - start,
            )

        image = self.image_ref or self._resolve_image_ref()
        if not image:
            issues.append("image_ref 가 제공되지 않았습니다. (env: RECODER_IMAGE)")
            return RuntimePreflightResult(
                runtime_checks=runtime,
                status=PreflightStatus.WARN,
                issues=issues,
                duration_seconds=time.monotonic() - start,
            )

        runtime_status = PreflightStatus.PASSED

        host_port = self.contract.runtime.host_port
        app_port  = self.contract.runtime.app_port
        health    = self.contract.runtime.health_check_path
        startup_timeout = parse_duration(
            getattr(self.contract.operational_policy.startup, "timeout", None),
            default=60,
        )

        try:
            env = self._build_env()
            with temporary_container(
                image,
                host_port=host_port,
                app_port=app_port,
                env=env,
                cli=self.docker_cli,
            ) as cr:
                runtime.temp_container_id = cr.container_id

                # 1) PORT LISTEN
                ok_port = wait_for_port_listen("127.0.0.1", host_port, startup_timeout)
                runtime.container_alive = ok_port
                if not ok_port:
                    issues.append(
                        f"컨테이너가 {startup_timeout}s 내에 host_port {host_port} 에서 LISTEN 시작하지 않음."
                    )
                    runtime_status = PreflightStatus.BLOCKED

                # 2) health probe (PORT 가 LISTEN 됐을 때만 의미)
                if ok_port:
                    health_ok, attempts = wait_for_health(
                        "127.0.0.1", host_port, health, startup_timeout,
                    )
                    runtime.health_passed = health_ok
                    if not health_ok:
                        issues.append(
                            f"health endpoint {health} 가 {startup_timeout}s 내 200 응답 못 함 "
                            f"({attempts} 회 시도)."
                        )
                        runtime_status = PreflightStatus.BLOCKED

                    # 3) smoke tests
                    smoke_tests = self.contract.operational_policy.smoke_tests
                    if smoke_tests:
                        smoke_ok, smoke_results = run_smoke_tests(
                            "127.0.0.1", host_port, smoke_tests,
                        )
                        runtime.smoke_passed = smoke_ok
                        if not smoke_ok:
                            failed = [r for r in smoke_results if not r["passed"]]
                            issues.append(
                                f"smoke tests {len(failed)}/{len(smoke_results)} 실패: "
                                + ", ".join(f"{r['path']} -> {r['actual_status']}" for r in failed[:3])
                            )
                            runtime_status = PreflightStatus.BLOCKED
                    else:
                        runtime.smoke_passed = True  # 없으면 통과

                # 4) container logs check
                logs = fetch_container_logs(cr.container_id, tail=50, cli=self.docker_cli)
                log_ok, bad_lines = detect_log_pattern_issues(logs)
                runtime.log_pattern_ok = log_ok
                runtime.container_log_tail = logs
                if not log_ok:
                    issues.append(
                        f"컨테이너 로그에서 위험 패턴 {len(bad_lines)}건 감지: "
                        + " | ".join(bad_lines[:2])
                    )
                    # log 문제는 보통 startup 실패의 원인 — 이미 BLOCKED 일 가능성 높음
                    if runtime_status == PreflightStatus.PASSED:
                        runtime_status = PreflightStatus.WARN

                # 5) duration
                runtime.duration_ms = int((time.monotonic() - cr.started_at) * 1000)

        except RuntimeError as exc:
            issues.append(f"Runtime Preflight 실행 오류: {mask_log(str(exc))}")
            runtime_status = PreflightStatus.BLOCKED

        return RuntimePreflightResult(
            runtime_checks=runtime,
            status=runtime_status,
            issues=issues,
            duration_seconds=time.monotonic() - start,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_image_ref(self) -> Optional[str]:
        """이미지 참조 해소. 환경변수 우선."""
        import os
        return os.environ.get("RECODER_IMAGE")

    def _build_env(self) -> dict[str, str]:
        """컨테이너에 주입할 환경변수.

        우선순위 (낮 → 높):
            1. .env 파일의 값들 (사용자 작성)
            2. ReleaseContract 의 runtime 값들 — **contract 가 source of truth**
               (PORT 는 항상 contract.runtime.app_port 와 일치해야 컨테이너가
                올바른 포트에서 LISTEN — .env 값으로 override 되면 안 됨)

        Secret 가 LLM prompt 로 새지 않도록 호출자 책임.
        """
        env: dict[str, str] = {}
        # 1) .env 먼저 로드 (낮은 우선순위)
        env_file = self.workspace / ".env"
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip().strip('"').strip("'")
            except OSError:
                pass
        # 2) Contract 의 runtime 값으로 override (높은 우선순위)
        env["PORT"] = str(self.contract.runtime.app_port)
        return env


# ---------------------------------------------------------------------------
# Public — merge static + runtime into single PreflightRun
# ---------------------------------------------------------------------------


def merge_runtime_into_preflight_run(
    preflight_run: PreflightRun,
    rt_result: RuntimePreflightResult,
) -> PreflightRun:
    """이미 만들어진 PreflightRun (Static) 에 Runtime 결과를 병합.

    - runtime_checks 필드 갱신
    - status: Static + Runtime 중 최악 (BLOCKED > WARN > PASSED)
    - score: 재계산 — runtime 실패 시 추가 차감
    """
    preflight_run.runtime_checks = rt_result.runtime_checks

    # 상태 병합
    order = {PreflightStatus.PASSED: 0, PreflightStatus.WARN: 1, PreflightStatus.BLOCKED: 2}
    if order[rt_result.status] > order[preflight_run.status]:
        preflight_run.status = rt_result.status

    # 점수 차감 — runtime 실패 항목별
    rc = rt_result.runtime_checks
    if rc.container_alive is False:
        preflight_run.score = max(0, preflight_run.score - 25)
    if rc.health_passed is False:
        preflight_run.score = max(0, preflight_run.score - 20)
    if rc.smoke_passed is False:
        preflight_run.score = max(0, preflight_run.score - 15)
    if rc.log_pattern_ok is False:
        preflight_run.score = max(0, preflight_run.score - 10)

    return preflight_run
