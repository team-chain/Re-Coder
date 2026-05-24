"""
코드 / 엔드포인트 정적 검사 (§30.1).

검사 2종:
  - MISSING_HEALTH_ENDPOINT  : recoder.yml 의 health_check_path 가 코드에 실제 정의됐는지
  - APP_ENTRYPOINT_NOT_FOUND : main.py / app.py / index.js 등 진입점 존재 여부

AST 기반 탐지는 First Run Wizard (D 영역) 의 본 작업. 본 검사는 텍스트 패턴
매칭으로 단순화 — 정확도는 떨어지지만 "있는지 없는지" 수준은 충분.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

try:
    from preflight import CheckResult
    from schemas import (
        ContractStack,
        PreflightBlocker,
        PreflightCheckCode,
        PreflightSeverity,
        PreflightWarning,
        ReleaseContract,
    )
except ImportError:  # pragma: no cover
    from core.preflight import CheckResult  # type: ignore
    from core.schemas import (  # type: ignore
        ContractStack,
        PreflightBlocker,
        PreflightCheckCode,
        PreflightSeverity,
        PreflightWarning,
        ReleaseContract,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PYTHON_FILES_GLOB = ("*.py",)
_NODE_FILES_GLOB = ("*.js", "*.ts", "*.mjs")

#: 진입점 후보 파일들. 스택별로 우선순위.
_ENTRYPOINT_CANDIDATES: dict[ContractStack, tuple[str, ...]] = {
    ContractStack.PYTHON_FASTAPI: ("main.py", "app.py", "app/main.py", "src/main.py"),
    ContractStack.PYTHON_FLASK:   ("app.py", "main.py", "wsgi.py", "src/app.py"),
    ContractStack.NODE_EXPRESS:   ("index.js", "server.js", "app.js", "src/index.js", "src/server.js"),
    ContractStack.NODE_NEXT:      ("next.config.js", "next.config.mjs", "pages/_app.js", "pages/_app.tsx", "app/page.tsx"),
    ContractStack.CUSTOM:         ("main.py", "app.py", "index.js", "server.js", "main.go", "main.rs"),
}


def _iter_source_files(
    workspace: Path, patterns: tuple[str, ...], max_files: int = 200
) -> list[Path]:
    """workspace 안에서 패턴에 매칭되는 파일 (재귀). node_modules / .venv / dist / .git 스킵."""
    skipped_dirs = {
        "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
        ".git", ".idea", ".vscode", "out", "coverage", ".pytest_cache",
    }
    found: list[Path] = []
    for pattern in patterns:
        for path in workspace.rglob(pattern):
            if any(part in skipped_dirs for part in path.parts):
                continue
            if len(found) >= max_files:
                return found
            found.append(path)
    return found


# ---------------------------------------------------------------------------
# 1. MISSING_HEALTH_ENDPOINT
# ---------------------------------------------------------------------------


# 각 프레임워크에서 라우터 정의를 인식하는 정규식 — 단순 패턴.
_HEALTH_PATTERNS: dict[ContractStack, list[re.Pattern[str]]] = {
    ContractStack.PYTHON_FASTAPI: [
        # @app.get("/health"), @router.get("/health")
        re.compile(r"""@\w+\.(?:get|post|api_route)\s*\(\s*['"]([^'"]+)['"]""", re.IGNORECASE),
    ],
    ContractStack.PYTHON_FLASK: [
        # @app.route("/health")
        re.compile(r"""@\w+\.route\s*\(\s*['"]([^'"]+)['"]""", re.IGNORECASE),
    ],
    ContractStack.NODE_EXPRESS: [
        # app.get("/health"), router.get("/health")
        re.compile(r"""\b(?:app|router)\.(?:get|post|use)\s*\(\s*['"]([^'"]+)['"]""", re.IGNORECASE),
    ],
    ContractStack.NODE_NEXT: [
        # Next.js API routes: pages/api/health.js, app/api/health/route.ts
    ],
    ContractStack.CUSTOM: [],
}


def _route_paths_in_files(
    workspace: Path, stack: ContractStack
) -> set[str]:
    """소스 코드에서 추출한 라우트 경로 집합 (간이 grep)."""
    patterns = _HEALTH_PATTERNS.get(stack, [])
    if not patterns:
        return set()

    if stack in {ContractStack.PYTHON_FASTAPI, ContractStack.PYTHON_FLASK}:
        files = _iter_source_files(workspace, _PYTHON_FILES_GLOB)
    elif stack == ContractStack.NODE_EXPRESS:
        files = _iter_source_files(workspace, _NODE_FILES_GLOB)
    else:
        files = []

    paths: set[str] = set()
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in patterns:
            for m in pat.finditer(text):
                paths.add(m.group(1))
    return paths


def _next_health_exists(workspace: Path, health_path: str) -> bool:
    """Next.js 의 health 라우트 (pages/api/health 또는 app/api/health/route) 탐지."""
    # 정규화
    clean = health_path.lstrip("/").rstrip("/")
    candidates = [
        workspace / "pages" / "api" / f"{clean}.js",
        workspace / "pages" / "api" / f"{clean}.ts",
        workspace / "pages" / "api" / clean / "index.js",
        workspace / "pages" / "api" / clean / "index.ts",
        workspace / "app" / "api" / clean / "route.js",
        workspace / "app" / "api" / clean / "route.ts",
    ]
    return any(c.exists() for c in candidates)


def check_missing_health_endpoint(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """recoder.yml 의 health_check_path 가 실제 코드에 라우터로 정의됐는지."""
    start = time.monotonic()
    stack = contract.project.stack
    health_path = contract.runtime.health_check_path

    details: dict = {
        "stack": stack.value,
        "health_check_path": health_path,
    }

    if stack == ContractStack.CUSTOM:
        # custom 은 라우트 형식이 천차만별 — 경고만.
        return CheckResult(
            code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            warning=PreflightWarning(
                code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
                message="custom 스택은 health 경로 자동 검증 불가. 직접 확인하세요.",
                severity=PreflightSeverity.LOW,
            ),
            details=details,
        )

    if stack == ContractStack.NODE_NEXT:
        found = _next_health_exists(workspace, health_path)
        details["next_route_found"] = found
        if found:
            return CheckResult(
                code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
                passed=True,
                duration_ms=int((time.monotonic() - start) * 1000),
                details=details,
            )
        return CheckResult(
            code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
            passed=False,
            duration_ms=int((time.monotonic() - start) * 1000),
            blocker=PreflightBlocker(
                code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
                message=f"Next.js API 라우트 {health_path} 를 찾을 수 없습니다.",
                fix_hint=f"pages/api{health_path}.ts 또는 app/api{health_path}/route.ts 를 추가하세요.",
                remediation_available=True,
                severity=PreflightSeverity.HIGH,
            ),
            details=details,
        )

    # Python (FastAPI/Flask) / Node Express
    found_paths = _route_paths_in_files(workspace, stack)
    details["routes_found"] = sorted(found_paths)[:30]  # 상위 30개만 details에 (로그 비대화 방지)

    # 정규화 — trailing slash 차이 흡수
    def _norm(s: str) -> str:
        return "/" + s.strip("/").lower() if s else "/"

    needle = _norm(health_path)
    matched = needle in {_norm(p) for p in found_paths}

    if matched:
        return CheckResult(
            code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    return CheckResult(
        code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        blocker=PreflightBlocker(
            code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
            message=f"{stack.value} 코드에 {health_path} 라우터가 정의돼 있지 않습니다.",
            fix_hint=(
                f"진입점에 다음과 같은 코드를 추가하세요:\n"
                f"  @app.get('{health_path}')\n"
                f"  def health():\n"
                f"      return {{'status': 'ok'}}"
            ),
            remediation_available=True,
            severity=PreflightSeverity.HIGH,
        ),
        details=details,
    )


# ---------------------------------------------------------------------------
# 2. APP_ENTRYPOINT_NOT_FOUND
# ---------------------------------------------------------------------------


def check_app_entrypoint(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """진입점 파일 (main.py / app.py / index.js 등) 이 워크스페이스에 존재하는지.

    스택별 후보 리스트를 순회하며 첫 매치를 찾는다.
    """
    start = time.monotonic()
    stack = contract.project.stack
    candidates = _ENTRYPOINT_CANDIDATES.get(stack, _ENTRYPOINT_CANDIDATES[ContractStack.CUSTOM])

    found: Optional[str] = None
    for rel in candidates:
        if (workspace / rel).exists():
            found = rel
            break

    details = {
        "stack": stack.value,
        "candidates": list(candidates),
        "found": found,
    }

    if found:
        return CheckResult(
            code=PreflightCheckCode.APP_ENTRYPOINT_NOT_FOUND,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    return CheckResult(
        code=PreflightCheckCode.APP_ENTRYPOINT_NOT_FOUND,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        blocker=PreflightBlocker(
            code=PreflightCheckCode.APP_ENTRYPOINT_NOT_FOUND,
            message=f"{stack.value} 진입점 파일을 찾을 수 없습니다.",
            fix_hint=f"다음 후보 중 하나를 생성하세요: {', '.join(candidates[:3])}",
            remediation_available=False,  # 자동 생성은 위험 — 사용자 가이드만
            severity=PreflightSeverity.HIGH,
        ),
        details=details,
    )
