"""
포트 / 네트워크 정적 검사 (§30.1).

검사 2종:
  - HOST_PORT_CONFLICT  : recoder.yml 의 host_port 가 로컬에서 사용 중인지
  - APP_PORT_MISMATCH    : recoder.yml 의 app_port 와 Dockerfile EXPOSE 일치 여부
"""

from __future__ import annotations

import re
import socket
import time
from pathlib import Path

try:
    from preflight import CheckResult
    from preflight.checks.docker_checks import find_dockerfile
    from schemas import (
        PreflightBlocker,
        PreflightCheckCode,
        PreflightSeverity,
        PreflightWarning,
        ReleaseContract,
    )
except ImportError:  # pragma: no cover
    from core.preflight import CheckResult  # type: ignore
    from core.preflight.checks.docker_checks import find_dockerfile  # type: ignore
    from core.schemas import (  # type: ignore
        PreflightBlocker,
        PreflightCheckCode,
        PreflightSeverity,
        PreflightWarning,
        ReleaseContract,
    )


# ---------------------------------------------------------------------------
# 1. HOST_PORT_CONFLICT
# ---------------------------------------------------------------------------


def _port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """주어진 host:port 가 이미 LISTEN 중인지 (간이 체크).

    소켓 connect 가 성공 = 누군가 듣고 있음 = 사용 중.
    실패 (ConnectionRefused) = 비어있음.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            return True
    except (ConnectionRefusedError, OSError):
        return False


def check_host_port_conflict(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """host_port 가 로컬에서 이미 사용 중이면 차단.

    Runtime Preflight (B 영역) 가 같은 포트로 임시 컨테이너 띄울 텐데 충돌나면
    실패하므로 정적으로 미리 차단.
    """
    start = time.monotonic()
    host_port = contract.runtime.host_port

    in_use = _port_in_use(host_port)

    details = {
        "host_port": host_port,
        "in_use": in_use,
    }

    if not in_use:
        return CheckResult(
            code=PreflightCheckCode.HOST_PORT_CONFLICT,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    return CheckResult(
        code=PreflightCheckCode.HOST_PORT_CONFLICT,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        blocker=PreflightBlocker(
            code=PreflightCheckCode.HOST_PORT_CONFLICT,
            message=f"호스트 포트 {host_port} 가 이미 사용 중입니다.",
            fix_hint=f"recoder.yml 의 runtime.host_port 를 다른 값으로 변경하거나 "
                     f"포트를 사용 중인 프로세스를 종료하세요. "
                     f"(Windows: netstat -ano | findstr :{host_port})",
            remediation_available=True,
            severity=PreflightSeverity.HIGH,
        ),
        details=details,
    )


# ---------------------------------------------------------------------------
# 2. APP_PORT_MISMATCH
# ---------------------------------------------------------------------------


_EXPOSE_RE: re.Pattern[str] = re.compile(
    r"^\s*EXPOSE\s+(\d+(?:/(?:tcp|udp))?(?:\s+\d+(?:/(?:tcp|udp))?)*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_exposed_ports(dockerfile_text: str) -> list[int]:
    """Dockerfile 의 EXPOSE 지시문에서 포트 번호 추출.

    EXPOSE 8000/tcp 9000/udp 같이 여러 포트 + 프로토콜 처리.
    """
    ports: list[int] = []
    for m in _EXPOSE_RE.finditer(dockerfile_text):
        for token in m.group(1).split():
            num = token.split("/", 1)[0]
            try:
                p = int(num)
                if 1 <= p <= 65535:
                    ports.append(p)
            except ValueError:
                continue
    return ports


def check_app_port_mismatch(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """recoder.yml 의 app_port 가 Dockerfile EXPOSE 와 일치하는지.

    Dockerfile 이 없으면 본 검사는 PASS (MISSING_DOCKERFILE 가 별도로 잡음).
    EXPOSE 지시문 자체가 없으면 warning (필수는 아니지만 권장).
    """
    start = time.monotonic()
    app_port = contract.runtime.app_port
    df = find_dockerfile(workspace)

    details: dict = {"app_port": app_port}

    if df is None:
        details["reason"] = "Dockerfile not found (handled by MISSING_DOCKERFILE)"
        return CheckResult(
            code=PreflightCheckCode.APP_PORT_MISMATCH,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    try:
        text = df.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        details["read_error"] = str(exc)
        return CheckResult(
            code=PreflightCheckCode.APP_PORT_MISMATCH,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    exposed = _extract_exposed_ports(text)
    details["exposed_ports"] = exposed

    if not exposed:
        # EXPOSE 자체가 없음 — 권장사항 위반이지만 차단까지는 아님
        return CheckResult(
            code=PreflightCheckCode.APP_PORT_MISMATCH,
            passed=False,
            duration_ms=int((time.monotonic() - start) * 1000),
            warning=PreflightWarning(
                code=PreflightCheckCode.APP_PORT_MISMATCH,
                message="Dockerfile 에 EXPOSE 지시문이 없습니다. 문서화 / 도구 호환을 위해 추가 권장.",
                fix_hint=f"Dockerfile 에 EXPOSE {app_port} 한 줄을 추가하세요.",
                severity=PreflightSeverity.LOW,
            ),
            details=details,
        )

    if app_port in exposed:
        return CheckResult(
            code=PreflightCheckCode.APP_PORT_MISMATCH,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    return CheckResult(
        code=PreflightCheckCode.APP_PORT_MISMATCH,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        blocker=PreflightBlocker(
            code=PreflightCheckCode.APP_PORT_MISMATCH,
            message=f"recoder.yml app_port={app_port} 인데 Dockerfile EXPOSE 는 {exposed} 입니다.",
            fix_hint=(
                f"둘 중 하나를 일치시키세요:\n"
                f"  - Dockerfile 에 EXPOSE {app_port} 추가\n"
                f"  - recoder.yml runtime.app_port 를 {exposed[0]} 로 변경"
            ),
            remediation_available=True,
            severity=PreflightSeverity.MEDIUM,
        ),
        details=details,
    )
