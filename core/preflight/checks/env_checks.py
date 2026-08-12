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


def _find_gitignore(workspace: Path, max_levels: int = 6) -> Optional[Path]:
    """`.gitignore` 를 **위로 올라가며** 찾는다. 못 찾으면 None.

    `.gitignore` 는 보통 저장소 루트에 하나만 둔다. 그런데 모노레포에서는
    검사 대상이 `backend/` 같은 하위 폴더다. 그 폴더만 보면, 루트의
    `.gitignore` 가 `.env` 를 이미 무시하고 있는데도 "gitignore 파일이
    없습니다"로 막는다 — **사용자는 고칠 것이 없는데 막히는** 형태다.

    저장소 경계(`.git`)를 만나면 거기서 멈춘다. 남의 저장소나 홈 디렉터리의
    `.gitignore` 를 이 프로젝트의 것으로 오인하지 않기 위해서다.
    """
    try:
        current = workspace.resolve()
    except OSError:
        current = workspace
    for _ in range(max_levels):
        candidate = current / ".gitignore"
        if candidate.is_file():
            return candidate
        if (current / ".git").exists():
            return None          # 저장소 루트인데 없다 → 진짜로 없는 것
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def _gitignore_patterns(workspace: Path) -> list[str]:
    """`.gitignore` 의 패턴 목록 (빈 줄/주석 제외)."""
    gi = _find_gitignore(workspace)
    if gi is None:
        return []
    try:
        return [
            line.strip()
            for line in gi.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except OSError:
        return []


def _glob_to_regex(pattern: str, *, anchored: bool) -> str:
    """gitignore 글롭 → 정규식. `*` 는 `/` 를 넘지 않는다(git 규칙)."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(ch))
            else:
                out.append(pattern[i:close + 1])
                i = close + 1
                continue
        else:
            out.append(re.escape(ch))
        i += 1
    body = "".join(out)
    # 앵커가 없는 패턴은 어느 깊이에서나 맞는다.
    return body if anchored else rf"(?:.*/)?{body}"


def _env_pattern_matches(env_path: str, patterns: list[str]) -> bool:
    """`env_path` 가 gitignore 패턴에 걸리는지.

    **`env_path` 는 `.gitignore` 가 있는 폴더 기준 상대 경로다.** 파일 이름만
    넘기면 안 된다 — git 은 `/`로 시작하는 패턴을 `.gitignore` 가 있는 폴더에
    **고정**해서 적용하기 때문이다.

    저장소 루트의 `.gitignore` 에 `/.env` 만 있는 모노레포를 생각해 보자.
    이름만 비교하면 `backend/.env` 도 무시된다고 판단하지만, git 은 루트의
    `.env` 만 무시한다. 즉 **백엔드의 시크릿은 실제로 추적되는데 검사는
    통과**한다 — 오탐보다 나쁜, 놓치는 쪽의 오류다.

    git 규칙 중 이 검사에 필요한 것만 구현한다:
      · `/` 로 시작 → `.gitignore` 폴더에 고정
      · 중간에 `/` 포함 → 마찬가지로 고정
      · `/` 없음 → 어느 깊이의 이름에나 매칭
      · `!` 로 시작 → 부정. **마지막에 매칭된 규칙이 이긴다.**
      · 끝의 `/` 는 디렉터리 표시 — 여기서는 떼고 본다
    """
    target = (env_path or "").replace("\\", "/")
    # **`lstrip("./")` 을 쓰면 안 된다.** 문자 *집합*을 지우기 때문에
    # `.env` 가 `env` 로 깎여 모든 비교가 어긋난다. 접두사만 떼어낸다.
    while target.startswith("./"):
        target = target[2:]
    target = target.lstrip("/")
    if not target:
        return False

    # **조상 폴더도 함께 본다.** git 에서 어떤 폴더가 무시되면 그 아래
    # 전부가 무시된다 — `backend/` 한 줄이 `backend/.env` 를 덮는다.
    parts = target.split("/")
    candidates = [target] + ["/".join(parts[:i]) for i in range(1, len(parts))]

    ignored = False
    for raw in patterns:
        pattern = raw.strip()
        if not pattern:
            continue
        negate = pattern.startswith("!")
        if negate:
            pattern = pattern[1:]
        pattern = pattern.rstrip("/")
        if not pattern:
            continue
        if pattern.startswith("/"):
            anchored, pattern = True, pattern[1:]
        else:
            anchored = "/" in pattern
        try:
            regex = re.compile(_glob_to_regex(pattern, anchored=anchored))
        except re.error:
            continue
        if any(regex.fullmatch(c) for c in candidates):
            ignored = not negate
    return ignored


def _env_path_relative_to(workspace: Path, env_file: str, gitignore_path: Path) -> str:
    """검사 대상 env 파일을 **`.gitignore` 가 있는 폴더 기준** 상대 경로로.

    `.gitignore` 가 상위 폴더에 있으면 `backend/.env` 같은 경로가 된다.
    이 경로로 비교해야 `/`로 고정된 패턴을 git 과 같게 판정할 수 있다.
    """
    fallback = (env_file or "").replace("\\", "/")
    while fallback.startswith("./"):
        fallback = fallback[2:]
    try:
        env_abs = (workspace / env_file).resolve()
        return env_abs.relative_to(gitignore_path.parent.resolve()).as_posix()
    except (ValueError, OSError):
        return fallback


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
    # **존재 확인도 위로 올라가며** 한다. 여기만 `workspace / ".gitignore"` 를
    # 보면, 패턴은 상위에서 찾아 왔는데 "파일이 없다"고 막는 어긋남이 생긴다.
    gitignore_path = _find_gitignore(workspace)
    patterns = _gitignore_patterns(workspace)

    details = {
        "env_file": env_file,
        "gitignore_exists": gitignore_path is not None,
        "gitignore_path": str(gitignore_path) if gitignore_path else "",
        "patterns_count": len(patterns),
    }

    if gitignore_path is None:
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

    if _env_pattern_matches(_env_path_relative_to(workspace, env_file, gitignore_path), patterns):
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
