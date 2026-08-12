"""
환경 변수 / .env 파일 관련 정적 검사 (§30.1).

검사 3종:
  - MISSING_REQUIRED_ENV
  - ENV_FILE_NOT_GITIGNORED
  - INVALID_ENV_FORMAT

모두 동기 함수 (디스크 I/O 만). 호출자가 thread pool 에 위임 가능.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

try:
    from preflight import CheckResult, safe_relative_join
    from schemas import (
        PreflightBlocker,
        PreflightCheckCode,
        PreflightSeverity,
        PreflightWarning,
        ReleaseContract,
    )
except ImportError:  # pragma: no cover
    from core.preflight import CheckResult, safe_relative_join  # type: ignore
    from core.schemas import (  # type: ignore
        PreflightBlocker,
        PreflightCheckCode,
        PreflightSeverity,
        PreflightWarning,
        ReleaseContract,
    )


# ---------------------------------------------------------------------------
# 1. MISSING_REQUIRED_ENV
# ---------------------------------------------------------------------------


def parse_env_file(env_path: Path) -> dict[str, str]:
    """간단한 .env 파서. 빈 줄 / 주석 무시. quoting 일부 지원.

    형식 가정: ``KEY=VALUE`` (한 줄). VALUE 의 양 끝 작은/큰 따옴표는 제거.
    """
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    try:
        raw = env_path.read_text(encoding="utf-8")
    except OSError:
        return result
    for line_no, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        # 양 끝 따옴표 제거 (간단 처리)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def check_missing_required_env(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """recoder.yml 의 ``preflight.required_env`` 가 ``env_file`` 안에 모두 있는지 확인.

    빈 값은 "있다" 로 간주 (값 자체는 사용자 책임).
    env_file 이 없으면 모든 required_env 가 누락된 것으로 처리.
    """
    start = time.monotonic()
    required = list(contract.preflight.required_env)
    env_path = safe_relative_join(workspace, contract.runtime.env_file)

    details: dict = {
        "env_file": contract.runtime.env_file,
        "required_env": required,
    }

    if env_path is None or not env_path.exists():
        details["env_file_exists"] = False
        details["missing"] = required
        blocker = (
            PreflightBlocker(
                code=PreflightCheckCode.MISSING_REQUIRED_ENV,
                message=(
                    f"환경 파일 {contract.runtime.env_file!r} 이 없거나 접근 불가합니다. "
                    f"필수 환경변수 {len(required)}개를 정의할 수 없습니다."
                ),
                fix_hint=f"{contract.runtime.env_file} 을 생성하고 "
                         f"{', '.join(required)} 를 설정하세요.",
                remediation_available=True,
                severity=PreflightSeverity.HIGH,
            )
            if required
            else None
        )
        passed = not required  # required 가 비어있으면 통과
        return CheckResult(
            code=PreflightCheckCode.MISSING_REQUIRED_ENV,
            passed=passed,
            duration_ms=int((time.monotonic() - start) * 1000),
            blocker=blocker,
            details=details,
        )

    env_vars = parse_env_file(env_path)
    details["env_file_exists"] = True
    details["defined_keys_count"] = len(env_vars)

    missing = [k for k in required if k not in env_vars]
    details["missing"] = missing

    if not missing:
        return CheckResult(
            code=PreflightCheckCode.MISSING_REQUIRED_ENV,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    return CheckResult(
        code=PreflightCheckCode.MISSING_REQUIRED_ENV,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        blocker=PreflightBlocker(
            code=PreflightCheckCode.MISSING_REQUIRED_ENV,
            message=f"필수 환경변수 누락: {', '.join(missing)}",
            fix_hint=f"{contract.runtime.env_file} 에 다음을 추가하세요: "
                     + ", ".join(f"{k}=<value>" for k in missing),
            remediation_available=True,
            severity=PreflightSeverity.HIGH,
        ),
        details=details,
    )


# ---------------------------------------------------------------------------
# 2. ENV_FILE_NOT_GITIGNORED
# ---------------------------------------------------------------------------


def _gitignore_patterns(workspace: Path) -> list[str]:
    """workspace/.gitignore 의 패턴 목록 (빈 줄/주석 제외)."""
    gi = workspace / ".gitignore"
    if not gi.exists():
        return []
    try:
        return [
            line.strip()
            for line in gi.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except OSError:
        return []


def _env_pattern_matches(env_filename: str, patterns: list[str]) -> bool:
    """env_filename 이 gitignore 패턴들 중 하나에 매칭되는지.

    완전 정확한 gitignore 시맨틱은 아니지만 일반적인 케이스 (`.env`, `.env.*`,
    `*.env`, 절대경로) 는 처리.
    """
    base = env_filename.split("/")[-1]
    for p in patterns:
        if p in {env_filename, base, f"./{env_filename}", f"/{env_filename}"}:
            return True
        # 와일드카드 간이 처리
        if "*" in p:
            regex = re.escape(p).replace(r"\*", ".*")
            if re.fullmatch(regex, env_filename) or re.fullmatch(regex, base):
                return True
    return False


def check_env_file_gitignored(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """env_file 이 .gitignore 에 잡혀 있는지 확인.

    *Risk*: secret 이 git 추적되어 GitHub 등에 푸시되면 즉시 노출. 따라서 항상
    .gitignore 에 등록되어야 함.
    """
    start = time.monotonic()
    env_file = contract.runtime.env_file
    patterns = _gitignore_patterns(workspace)

    details = {
        "env_file": env_file,
        "gitignore_exists": (workspace / ".gitignore").exists(),
        "patterns_count": len(patterns),
    }

    if not (workspace / ".gitignore").exists():
        return CheckResult(
            code=PreflightCheckCode.ENV_FILE_NOT_GITIGNORED,
            passed=False,
            duration_ms=int((time.monotonic() - start) * 1000),
            blocker=PreflightBlocker(
                code=PreflightCheckCode.ENV_FILE_NOT_GITIGNORED,
                message=".gitignore 파일이 없습니다. env 파일이 추적될 위험이 있습니다.",
                fix_hint=f".gitignore 파일을 생성하고 {env_file} 을 추가하세요.",
                remediation_available=True,
                severity=PreflightSeverity.CRITICAL,
            ),
            details=details,
        )

    if _env_pattern_matches(env_file, patterns):
        return CheckResult(
            code=PreflightCheckCode.ENV_FILE_NOT_GITIGNORED,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    return CheckResult(
        code=PreflightCheckCode.ENV_FILE_NOT_GITIGNORED,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        blocker=PreflightBlocker(
            code=PreflightCheckCode.ENV_FILE_NOT_GITIGNORED,
            message=f"{env_file} 이 .gitignore 에 없습니다. secret 노출 위험.",
            fix_hint=f".gitignore 에 {env_file} 한 줄을 추가하세요. "
                     "이미 git 에 추적 중이면 'git rm --cached <file>' 도 필요할 수 있습니다.",
            remediation_available=True,
            severity=PreflightSeverity.CRITICAL,
        ),
        details=details,
    )


# ---------------------------------------------------------------------------
# 3. INVALID_ENV_FORMAT
# ---------------------------------------------------------------------------


# 변수 이름의 표준 규칙 — POSIX style: 영문/숫자/언더스코어, 첫 글자는 영문/언더스코어
_ENV_KEY_RE: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_invalid_env_format(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """env_file 의 line 단위 문법 검증.

    탐지하는 문제:
      - "=" 없는 줄 (주석/빈 줄 제외)
      - 잘못된 변수 이름 (숫자 시작 / 특수문자)
      - 닫히지 않은 따옴표

    env_file 이 없으면 통과 (MISSING_REQUIRED_ENV 가 별도로 잡음).
    """
    start = time.monotonic()
    env_file = contract.runtime.env_file
    env_path = safe_relative_join(workspace, env_file)
    details: dict = {"env_file": env_file, "issues": []}

    if env_path is None or not env_path.exists():
        # 파일 자체가 없는 건 다른 검사가 잡음. 본 검사는 PASS.
        return CheckResult(
            code=PreflightCheckCode.INVALID_ENV_FORMAT,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    try:
        raw = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        details["read_error"] = str(exc)
        return CheckResult(
            code=PreflightCheckCode.INVALID_ENV_FORMAT,
            passed=False,
            duration_ms=int((time.monotonic() - start) * 1000),
            blocker=PreflightBlocker(
                code=PreflightCheckCode.INVALID_ENV_FORMAT,
                message=f"{env_file} 파일을 읽을 수 없습니다.",
                fix_hint="파일 권한 / 인코딩 (UTF-8) 을 확인하세요.",
                severity=PreflightSeverity.HIGH,
            ),
            details=details,
        )

    issues: list[dict] = []
    for line_no, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            issues.append({"line": line_no, "kind": "no_equal", "snippet": line[:60]})
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            issues.append({"line": line_no, "kind": "invalid_key", "snippet": key[:60]})
            continue
        # 닫히지 않은 따옴표 탐지
        if value.count('"') % 2 != 0 or value.count("'") % 2 != 0:
            issues.append({"line": line_no, "kind": "unbalanced_quote", "snippet": value[:60]})

    details["issues"] = issues

    if not issues:
        return CheckResult(
            code=PreflightCheckCode.INVALID_ENV_FORMAT,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    # 가벼운 warning 으로 처리 (배포 자체는 가능하니까)
    return CheckResult(
        code=PreflightCheckCode.INVALID_ENV_FORMAT,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        warning=PreflightWarning(
            code=PreflightCheckCode.INVALID_ENV_FORMAT,
            message=f"{env_file} 에서 {len(issues)}개의 형식 문제가 발견됐습니다.",
            fix_hint="KEY=VALUE 형식 / 변수 이름 규칙 / 따옴표 균형을 확인하세요.",
            severity=PreflightSeverity.MEDIUM,
        ),
        details=details,
    )
