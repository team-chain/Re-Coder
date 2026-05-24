"""
의존성 / 보안 정적 검사 (§30.1).

검사 3종:
  - UNPINNED_DEPENDENCIES  : requirements.txt / package.json 의 미고정 의존성
  - CRITICAL_VULNERABILITY : Trivy / 기타 스캐너 결과 (Phase A-2 에서는 placeholder, B 영역과 통합)
  - SECRET_LEAK_RISK       : gitleaks 패턴 매칭 1차 (정확한 결과는 별도 컨테이너)
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

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
# 1. UNPINNED_DEPENDENCIES
# ---------------------------------------------------------------------------


def _analyze_requirements_txt(workspace: Path) -> tuple[int, int, list[str]]:
    """requirements.txt 분석. (총 개수, 미고정 개수, 미고정 패키지 목록) 반환.

    "미고정" = ``==`` 가 없는 줄. 단, ``-r``, ``--`` 시작은 무시 (옵션 줄).
    """
    path = workspace / "requirements.txt"
    if not path.exists():
        return 0, 0, []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0, 0, []
    total = 0
    unpinned: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        total += 1
        # 단순 룰: == 또는 === 또는 git+ssh, @ 가 있으면 고정으로 간주
        if "==" in line or "@ " in line or line.startswith("git+") or "===" in line:
            continue
        # 토큰 시작 = 패키지 이름 (대략)
        name_match = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        unpinned.append(name_match.group(1) if name_match else line[:40])
    return total, len(unpinned), unpinned


def _analyze_package_json(workspace: Path) -> tuple[int, int, list[str]]:
    """package.json 의 dependencies + devDependencies 미고정 개수.

    npm 의 ``^`` / ``~`` / ``>=`` / 무규칙(``*``) 은 모두 미고정.
    """
    path = workspace / "package.json"
    if not path.exists():
        return 0, 0, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0, []
    total = 0
    unpinned: list[str] = []
    for section in ("dependencies", "devDependencies"):
        deps = data.get(section) or {}
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            total += 1
            if not isinstance(spec, str):
                continue
            # 고정 = 숫자.숫자.숫자 (^ ~ > < * x 없음) 또는 git URL hash 명시
            spec_stripped = spec.strip()
            if re.fullmatch(r"\d+\.\d+\.\d+([+-][\w.]+)?", spec_stripped):
                continue
            if spec_stripped.startswith("file:") or "#" in spec_stripped:
                continue
            unpinned.append(name)
    return total, len(unpinned), unpinned


def check_unpinned_dependencies(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """requirements.txt / package.json 의 미고정 의존성 탐지.

    배포 재현성을 위해 모든 의존성은 정확한 버전 (``==`` / ``^`` 금지) 권장.
    완벽 차단까지는 아니고 warning.
    """
    start = time.monotonic()

    py_total, py_unpinned, py_list = _analyze_requirements_txt(workspace)
    node_total, node_unpinned, node_list = _analyze_package_json(workspace)

    total = py_total + node_total
    unpinned = py_unpinned + node_unpinned

    details = {
        "python": {"total": py_total, "unpinned": py_unpinned, "names": py_list[:20]},
        "node":   {"total": node_total, "unpinned": node_unpinned, "names": node_list[:20]},
    }

    if total == 0:
        # 어떤 매니페스트도 없으면 PASS
        details["reason"] = "no dependency manifest found"
        return CheckResult(
            code=PreflightCheckCode.UNPINNED_DEPENDENCIES,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    # 비율 기준: 미고정이 30% 이상이면 warning, 60% 이상이면 더 강한 warning
    ratio = unpinned / total if total else 0.0
    details["ratio"] = round(ratio, 3)

    if ratio == 0.0:
        return CheckResult(
            code=PreflightCheckCode.UNPINNED_DEPENDENCIES,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    severity = (
        PreflightSeverity.MEDIUM if ratio < 0.6 else PreflightSeverity.HIGH
    )
    return CheckResult(
        code=PreflightCheckCode.UNPINNED_DEPENDENCIES,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        warning=PreflightWarning(
            code=PreflightCheckCode.UNPINNED_DEPENDENCIES,
            message=f"미고정 의존성 {unpinned}/{total} ({ratio*100:.0f}%) — 재현성 위험.",
            fix_hint="pip freeze > requirements.txt 또는 package.json 의 ^/~ 를 정확한 버전으로 변경.",
            severity=severity,
        ),
        details=details,
    )


# ---------------------------------------------------------------------------
# 2. CRITICAL_VULNERABILITY (placeholder — B 영역 Trivy 통합 시 대체)
# ---------------------------------------------------------------------------


def check_critical_vulnerability(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """이미지/의존성 critical 취약점 탐지.

    Static Preflight 단계에서는 실제 Trivy 호출을 안 함 (느림 + Docker 필요).
    대신 직전 Trivy 결과 파일 (``~/.recoder/cache/trivy_<hash>.json``) 이 있으면
    참조. 없으면 SKIPPED 처리 + warning.

    B 영역의 Runtime Preflight 가 실제 Trivy 컨테이너를 띄우고 결과를 캐시하면
    본 검사가 그 캐시를 읽음.
    """
    start = time.monotonic()
    cache_dir = Path.home() / ".recoder" / "cache"
    contract_hash = (contract.contract_hash or "").replace("sha256:", "")[:16]
    cache_file = cache_dir / f"trivy_{contract_hash}.json"

    details: dict = {"cache_file": str(cache_file), "cache_exists": cache_file.exists()}

    if not cache_file.exists():
        # placeholder warning — 실제 스캔이 아직 안 됐다는 정보
        return CheckResult(
            code=PreflightCheckCode.CRITICAL_VULNERABILITY,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            warning=PreflightWarning(
                code=PreflightCheckCode.CRITICAL_VULNERABILITY,
                message="이미지 취약점 스캔 결과 캐시가 없습니다 (Runtime Preflight 미실행).",
                fix_hint="Runtime Preflight 또는 Trivy 통합 실행 후 재검사하세요.",
                severity=PreflightSeverity.LOW,
            ),
            details=details,
        )

    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        details["cache_read_error"] = str(exc)
        return CheckResult(
            code=PreflightCheckCode.CRITICAL_VULNERABILITY,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            warning=PreflightWarning(
                code=PreflightCheckCode.CRITICAL_VULNERABILITY,
                message="Trivy 캐시 읽기 실패.",
                severity=PreflightSeverity.LOW,
            ),
            details=details,
        )

    critical_count = int(cached.get("critical_count", 0))
    high_count = int(cached.get("high_count", 0))
    details["critical_count"] = critical_count
    details["high_count"] = high_count

    if critical_count == 0 and high_count == 0:
        return CheckResult(
            code=PreflightCheckCode.CRITICAL_VULNERABILITY,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    if critical_count > 0 and contract.preflight.block_on_critical_vuln:
        return CheckResult(
            code=PreflightCheckCode.CRITICAL_VULNERABILITY,
            passed=False,
            duration_ms=int((time.monotonic() - start) * 1000),
            blocker=PreflightBlocker(
                code=PreflightCheckCode.CRITICAL_VULNERABILITY,
                message=f"Critical 취약점 {critical_count}건 (high {high_count}건).",
                fix_hint="Trivy 결과 상세를 확인하고 패치 가능한 패키지를 업그레이드하세요.",
                remediation_available=False,
                severity=PreflightSeverity.CRITICAL,
            ),
            details=details,
        )

    # High 만 있거나 block 정책이 꺼져 있으면 warning
    return CheckResult(
        code=PreflightCheckCode.CRITICAL_VULNERABILITY,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        warning=PreflightWarning(
            code=PreflightCheckCode.CRITICAL_VULNERABILITY,
            message=f"취약점 critical={critical_count}, high={high_count}.",
            fix_hint="Trivy 결과를 검토하세요.",
            severity=PreflightSeverity.HIGH if critical_count else PreflightSeverity.MEDIUM,
        ),
        details=details,
    )


# ---------------------------------------------------------------------------
# 3. SECRET_LEAK_RISK
# ---------------------------------------------------------------------------


#: 잘 알려진 secret 패턴 — gitleaks 의 일부 룰을 단순화. 정확한 결과는 gitleaks 본체.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS_ACCESS_KEY", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GITHUB_TOKEN", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}")),
    ("STRIPE_KEY", re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{24,}")),
    ("SLACK_TOKEN", re.compile(r"xox[baprs]-[A-Za-z0-9\-]+")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")),
    ("PRIVATE_KEY_BLOCK", re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----")),
]

#: 검사 대상 — text 파일만. 바이너리는 스킵.
_TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".rb", ".php", ".yml", ".yaml", ".json", ".toml", ".ini",
    ".conf", ".env", ".sh", ".bash", ".zsh", ".sql", ".md", ".txt",
}


def _iter_text_files(workspace: Path, max_files: int = 500) -> list[Path]:
    skipped = {"node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".git"}
    found: list[Path] = []
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skipped for part in path.parts):
            continue
        if path.suffix.lower() not in _TEXT_EXTENSIONS:
            continue
        # 파일 크기 1MB 초과면 스킵 (대용량 로그 등)
        try:
            if path.stat().st_size > 1_000_000:
                continue
        except OSError:
            continue
        found.append(path)
        if len(found) >= max_files:
            break
    return found


def check_secret_leak_risk(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """프로젝트 내 텍스트 파일에서 secret 패턴 탐지.

    .env 파일도 검사 — secret 자체는 .env 에 있는 게 정상이지만, .env 가
    .gitignore 에 없으면 ENV_FILE_NOT_GITIGNORED 검사가 별도로 잡음.
    여기서는 단순 패턴 매칭 — gitleaks 만큼 정확하지 않지만 빠른 1차 방어선.

    *중요*: 발견된 secret 의 **원문은 details 에 절대 저장하지 않는다**.
    파일 경로 / 라인 / 패턴 이름만 기록.
    """
    start = time.monotonic()
    files = _iter_text_files(workspace)

    findings: list[dict] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, pattern in _SECRET_PATTERNS:
            for m in pattern.finditer(text):
                # 어느 라인인지 찾기
                line_no = text.count("\n", 0, m.start()) + 1
                findings.append({
                    "file": str(f.relative_to(workspace)),
                    "line": line_no,
                    "pattern": name,
                    # 의도적으로 m.group() 저장하지 않음 — secret 원문 보안
                })

    details = {
        "scanned_files": len(files),
        "findings_count": len(findings),
        "findings": findings[:20],  # 상위 20개만
    }

    if not findings:
        return CheckResult(
            code=PreflightCheckCode.SECRET_LEAK_RISK,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    # secret 발견 = 거의 항상 차단해야
    return CheckResult(
        code=PreflightCheckCode.SECRET_LEAK_RISK,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        blocker=PreflightBlocker(
            code=PreflightCheckCode.SECRET_LEAK_RISK,
            message=f"하드코딩된 secret 의심 패턴 {len(findings)}건 발견.",
            fix_hint=(
                "secret 을 환경변수 또는 secret manager 로 옮기고, "
                "이미 커밋된 경우 'git rm --cached' + 키 회전을 진행하세요. "
                "정확한 분석은 gitleaks 컨테이너 실행 결과를 참조."
            ),
            remediation_available=False,
            severity=PreflightSeverity.CRITICAL,
        ),
        details=details,
    )
