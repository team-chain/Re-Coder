"""
Docker 관련 정적 검사 (§30.1).

검사 2종:
  - MISSING_DOCKERFILE       : Dockerfile 존재 여부
  - DOCKERFILE_BUILD_RISK    : 빌드 실패 위험 패턴 탐지 (Hadolint 보조)

Hadolint 자체는 일회성 컨테이너로 실행 — Static Preflight 와 별개. 본 검사는
파일을 직접 읽고 잘 알려진 안티패턴을 정규식으로 탐지하는 빠른 1차 방어선.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

try:
    from preflight import CheckResult
    from schemas import (
        PreflightBlocker,
        PreflightCheckCode,
        PreflightSeverity,
        PreflightWarning,
        ReleaseContract,
    )
except ImportError:  # pragma: no cover
    from core.preflight import CheckResult  # type: ignore
    from core.schemas import (  # type: ignore
        PreflightBlocker,
        PreflightCheckCode,
        PreflightSeverity,
        PreflightWarning,
        ReleaseContract,
    )


# ---------------------------------------------------------------------------
# 1. MISSING_DOCKERFILE
# ---------------------------------------------------------------------------


_DOCKERFILE_CANDIDATES: tuple[str, ...] = ("Dockerfile", "dockerfile", "Containerfile")


def find_dockerfile(workspace: Path) -> Path | None:
    """워크스페이스 루트에서 Dockerfile 탐색. 없으면 None."""
    for name in _DOCKERFILE_CANDIDATES:
        candidate = workspace / name
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def check_missing_dockerfile(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """Dockerfile 이 워크스페이스에 존재하는지."""
    start = time.monotonic()
    df = find_dockerfile(workspace)

    details = {
        "candidates": list(_DOCKERFILE_CANDIDATES),
        "found": str(df.relative_to(workspace)) if df else None,
    }

    if df:
        return CheckResult(
            code=PreflightCheckCode.MISSING_DOCKERFILE,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    return CheckResult(
        code=PreflightCheckCode.MISSING_DOCKERFILE,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        blocker=PreflightBlocker(
            code=PreflightCheckCode.MISSING_DOCKERFILE,
            message="Dockerfile 이 없습니다.",
            fix_hint="recoder.yml 의 project.stack 에 맞춰 Dockerfile 을 생성하세요. "
                     "ReCoder Infra Agent 가 자동 생성할 수 있습니다.",
            remediation_available=True,
            severity=PreflightSeverity.HIGH,
        ),
        details=details,
    )


# ---------------------------------------------------------------------------
# 2. DOCKERFILE_BUILD_RISK
# ---------------------------------------------------------------------------


# 잘 알려진 위험 패턴 — 각 줄에 정규식 + 사람이 읽을 사유 + 심각도.
_DOCKERFILE_RISKS: list[tuple[re.Pattern[str], str, PreflightSeverity]] = [
    (
        # 태그(:) 와 다이제스트(@) 둘 다 없는 FROM (예: "FROM python"). 'scratch' 는 예외.
        # 'AS stage' 가 붙어도 매칭. 단, : 또는 @ 가 있으면 매칭 안 함.
        re.compile(
            r"^FROM\s+(?!scratch\b)[^\s:@]+(?:\s+AS\s+\w+)?\s*$",
            re.IGNORECASE | re.MULTILINE,
        ),
        "베이스 이미지 태그가 명시되지 않았습니다 (FROM ... 만). 'latest' 가 암묵 적용되어 재현성이 깨질 수 있습니다.",
        PreflightSeverity.MEDIUM,
    ),
    (
        re.compile(r"^FROM\s+\S+:latest\b", re.IGNORECASE | re.MULTILINE),
        "베이스 이미지 태그가 'latest' 입니다. 특정 버전(예: 3.11-slim)으로 고정하세요.",
        PreflightSeverity.MEDIUM,
    ),
    (
        re.compile(r"^USER\s+root\b", re.IGNORECASE | re.MULTILINE),
        "USER root 가 명시되어 있습니다. 런타임 권한을 최소화하려면 비root 사용자로 전환하세요.",
        PreflightSeverity.HIGH,
    ),
    (
        re.compile(r"\bADD\s+http", re.IGNORECASE),
        "ADD <URL> 패턴 사용 — 무결성 검증 불가. curl + checksum 또는 별도 단계로 분리 권장.",
        PreflightSeverity.MEDIUM,
    ),
    (
        re.compile(r"\bcurl\s+[^\n|]*\|\s*sh\b", re.IGNORECASE),
        "원격 스크립트를 검증 없이 실행 (curl | sh) — 공급망 공격 위험.",
        PreflightSeverity.CRITICAL,
    ),
    (
        re.compile(r"\bsudo\b", re.IGNORECASE),
        "Dockerfile 안에 sudo 사용 — 이미지 빌드 시 의도하지 않은 권한 상승.",
        PreflightSeverity.MEDIUM,
    ),
    (
        re.compile(r"\b(?:--break-system-packages|--no-build-isolation)\b"),
        "pip 격리 우회 옵션 사용 — 시스템 Python 오염 가능성. venv 또는 별도 경로 권장.",
        PreflightSeverity.LOW,
    ),
    (
        re.compile(r"^COPY\s+\.\s+/(?:[^/\s]|$)", re.IGNORECASE | re.MULTILINE),
        "COPY . / — 전체 워크스페이스를 컨테이너 / 에 복사 (불필요한 파일/시크릿 포함 위험). "
        ".dockerignore 또는 명시 경로 권장.",
        PreflightSeverity.MEDIUM,
    ),
]


def check_dockerfile_build_risk(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """Dockerfile 의 빌드 / 보안 위험 패턴 탐지.

    Dockerfile 이 없으면 MISSING_DOCKERFILE 가 별도로 잡으므로 본 검사는 PASS.
    """
    start = time.monotonic()
    df = find_dockerfile(workspace)

    if df is None:
        return CheckResult(
            code=PreflightCheckCode.DOCKERFILE_BUILD_RISK,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details={"reason": "Dockerfile not found (handled by MISSING_DOCKERFILE)"},
        )

    try:
        content = df.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return CheckResult(
            code=PreflightCheckCode.DOCKERFILE_BUILD_RISK,
            passed=False,
            duration_ms=int((time.monotonic() - start) * 1000),
            warning=PreflightWarning(
                code=PreflightCheckCode.DOCKERFILE_BUILD_RISK,
                message=f"Dockerfile 읽기 실패: {exc}",
                severity=PreflightSeverity.MEDIUM,
            ),
            details={"read_error": str(exc)},
        )

    findings: list[dict] = []
    max_sev = PreflightSeverity.LOW
    sev_order = {
        PreflightSeverity.LOW: 0,
        PreflightSeverity.MEDIUM: 1,
        PreflightSeverity.HIGH: 2,
        PreflightSeverity.CRITICAL: 3,
    }
    for pattern, reason, sev in _DOCKERFILE_RISKS:
        for m in pattern.finditer(content):
            findings.append(
                {
                    "line_offset": m.start(),
                    "snippet": m.group(0)[:80],
                    "reason": reason,
                    "severity": sev.value,
                }
            )
            if sev_order[sev] > sev_order[max_sev]:
                max_sev = sev

    details = {
        "dockerfile": str(df.relative_to(workspace)),
        "findings_count": len(findings),
        "findings": findings[:15],
    }

    if not findings:
        return CheckResult(
            code=PreflightCheckCode.DOCKERFILE_BUILD_RISK,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    if max_sev == PreflightSeverity.CRITICAL:
        return CheckResult(
            code=PreflightCheckCode.DOCKERFILE_BUILD_RISK,
            passed=False,
            duration_ms=int((time.monotonic() - start) * 1000),
            blocker=PreflightBlocker(
                code=PreflightCheckCode.DOCKERFILE_BUILD_RISK,
                message=f"Dockerfile 에 critical 위험 {sum(1 for f in findings if f['severity']=='critical')}건 포함.",
                fix_hint="curl | sh 등의 검증 없는 원격 스크립트 실행을 제거하세요.",
                remediation_available=False,
                severity=PreflightSeverity.CRITICAL,
            ),
            details=details,
        )

    return CheckResult(
        code=PreflightCheckCode.DOCKERFILE_BUILD_RISK,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        warning=PreflightWarning(
            code=PreflightCheckCode.DOCKERFILE_BUILD_RISK,
            message=f"Dockerfile 에 {len(findings)}건의 위험 패턴이 발견됐습니다.",
            fix_hint="Hadolint 또는 ReCoder Infra Agent 의 권고를 적용하세요.",
            severity=max_sev,
        ),
        details=details,
    )
