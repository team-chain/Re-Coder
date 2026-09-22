"""
ReCoder Static Preflight Runner (§30).

12종 정적 검사를 모두 실행하고 결과를 종합해 :class:`PreflightRun` 으로 반환.

검사는 thread pool 에서 병렬 실행 (디스크 I/O 위주 + 일부 socket connect).
모든 검사는 동기 함수 — async wrapper 는 본 모듈에서 제공.

사용:
    from preflight import run_static_preflight, StaticPreflightRunner

    # 비동기 (FastAPI 핸들러용)
    result: PreflightRun = await run_static_preflight(workspace_path, contract)

    # 동기 (CLI / 테스트용)
    runner = StaticPreflightRunner(workspace_path, contract)
    result = runner.run_sync()
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

try:
    from preflight import CheckResult, safe_workspace_path
    from preflight.checks.code_checks import (
        check_app_entrypoint,
        check_missing_health_endpoint,
    )
    from preflight.checks.deps_checks import (
        check_critical_vulnerability,
        check_secret_leak_risk,
        check_unpinned_dependencies,
    )
    from preflight.checks.docker_checks import (
        check_dockerfile_build_risk,
        check_missing_dockerfile,
    )
    from preflight.checks.env_checks import (
        check_env_file_gitignored,
        check_invalid_env_format,
        check_missing_required_env,
    )
    from preflight.checks.port_checks import (
        check_app_port_mismatch,
        check_host_port_conflict,
    )
    from schemas import (
        PreflightCheckCode,
        PreflightRun,
        PreflightStaticChecks,
        PreflightStatus,
        ReleaseContract,
    )
except ImportError:  # pragma: no cover
    from core.preflight import CheckResult, safe_workspace_path  # type: ignore
    from core.preflight.checks.code_checks import (  # type: ignore
        check_app_entrypoint,
        check_missing_health_endpoint,
    )
    from core.preflight.checks.deps_checks import (  # type: ignore
        check_critical_vulnerability,
        check_secret_leak_risk,
        check_unpinned_dependencies,
    )
    from core.preflight.checks.docker_checks import (  # type: ignore
        check_dockerfile_build_risk,
        check_missing_dockerfile,
    )
    from core.preflight.checks.env_checks import (  # type: ignore
        check_env_file_gitignored,
        check_invalid_env_format,
        check_missing_required_env,
    )
    from core.preflight.checks.port_checks import (  # type: ignore
        check_app_port_mismatch,
        check_host_port_conflict,
    )
    from core.schemas import (  # type: ignore
        PreflightCheckCode,
        PreflightRun,
        PreflightStaticChecks,
        PreflightStatus,
        ReleaseContract,
    )


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 검사 함수 레지스트리 — 추가/제거 시 한 곳에서 관리
# ---------------------------------------------------------------------------

CheckFn = Callable[[Path, ReleaseContract], CheckResult]


CHECK_REGISTRY: list[tuple[PreflightCheckCode, CheckFn]] = [
    # 환경 (3)
    (PreflightCheckCode.MISSING_REQUIRED_ENV,    check_missing_required_env),
    (PreflightCheckCode.ENV_FILE_NOT_GITIGNORED, check_env_file_gitignored),
    (PreflightCheckCode.INVALID_ENV_FORMAT,      check_invalid_env_format),
    # 코드 (2)
    (PreflightCheckCode.MISSING_HEALTH_ENDPOINT, check_missing_health_endpoint),
    (PreflightCheckCode.APP_ENTRYPOINT_NOT_FOUND, check_app_entrypoint),
    # Docker (2)
    (PreflightCheckCode.MISSING_DOCKERFILE,      check_missing_dockerfile),
    (PreflightCheckCode.DOCKERFILE_BUILD_RISK,   check_dockerfile_build_risk),
    # 포트 (2)
    (PreflightCheckCode.HOST_PORT_CONFLICT,      check_host_port_conflict),
    (PreflightCheckCode.APP_PORT_MISMATCH,       check_app_port_mismatch),
    # 의존성 / 보안 (3)
    (PreflightCheckCode.UNPINNED_DEPENDENCIES,   check_unpinned_dependencies),
    (PreflightCheckCode.CRITICAL_VULNERABILITY,  check_critical_vulnerability),
    (PreflightCheckCode.SECRET_LEAK_RISK,        check_secret_leak_risk),
]

assert len(CHECK_REGISTRY) == 12, "Static Preflight 는 정확히 12종 검사여야 함."


# ---------------------------------------------------------------------------
# Scoring — 0~100
# ---------------------------------------------------------------------------


def compute_score(results: list[CheckResult]) -> int:
    """검사 결과 12종으로부터 0~100 점수 산출.

    가중치:
      - blocker (CRITICAL): -25
      - blocker (HIGH):     -15
      - blocker (MEDIUM):   -10
      - blocker (LOW):       -5
      - warning (각 severity): blocker 의 절반
    기본 100 에서 차감. 0 미만은 0.
    """
    score = 100
    sev_weights = {
        "critical": 25,
        "high": 15,
        "medium": 10,
        "low": 5,
    }
    for r in results:
        if r.passed:
            continue
        if r.blocker:
            sev = r.blocker.severity.value if hasattr(r.blocker.severity, "value") else str(r.blocker.severity)
            score -= sev_weights.get(sev, 10)
        elif r.warning:
            sev = r.warning.severity.value if hasattr(r.warning.severity, "value") else str(r.warning.severity)
            score -= sev_weights.get(sev, 5) // 2
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Status 결정
# ---------------------------------------------------------------------------


def determine_status(results: list[CheckResult]) -> PreflightStatus:
    """Blockers 가 있으면 BLOCKED, warnings 만 있으면 WARN, 아니면 PASSED."""
    has_blockers = any(r.blocker for r in results if not r.passed)
    has_warnings = any(r.warning for r in results if not r.passed)
    if has_blockers:
        return PreflightStatus.BLOCKED
    if has_warnings:
        return PreflightStatus.WARN
    return PreflightStatus.PASSED


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class StaticPreflightRunner:
    """
    Static Preflight 실행기.

    Args:
        workspace_path: 프로젝트 루트 (절대 경로). ``preflight.safe_workspace_path``
            로 정규화됨.
        contract: 검사 기준이 되는 :class:`ReleaseContract`.
        project_id: PreflightRun.project_id 에 들어갈 식별자.
        max_workers: thread pool 크기 (기본 4 — 검사 12종이지만 I/O bound 라 충분).
    """

    def __init__(
        self,
        workspace_path: str,
        contract: ReleaseContract,
        project_id: Optional[str] = None,
        max_workers: int = 4,
    ) -> None:
        self.workspace = safe_workspace_path(workspace_path)
        self.contract = contract
        self.project_id = project_id
        self.max_workers = max_workers

    # ------------------------------------------------------------------
    # 동기 진입점
    # ------------------------------------------------------------------

    def run_sync(
        self, check_codes: Optional[set[PreflightCheckCode]] = None,
    ) -> PreflightRun:
        """선택한 검사만 thread pool 로 병렬 실행 + 종합.

        ``None``이면 기존처럼 12종 전체를 실행한다. 배포 대상 선택 전에는
        S3와 무관한 컨테이너 검사를 뒤로 미룰 수 있도록 선택 집합을 받는다.
        """
        registry = [
            (code, fn) for code, fn in CHECK_REGISTRY
            if check_codes is None or code in check_codes
        ]
        results: list[CheckResult] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._safe_invoke, fn, code): code
                for code, fn in registry
            }
            for future in futures:
                results.append(future.result())

        return self._aggregate(results, registry)

    # ------------------------------------------------------------------
    # 비동기 진입점 (FastAPI 핸들러용)
    # ------------------------------------------------------------------

    async def run_async(
        self, check_codes: Optional[set[PreflightCheckCode]] = None,
    ) -> PreflightRun:
        """thread pool 실행을 async wrapper 로."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.run_sync, check_codes)

    # ------------------------------------------------------------------
    # 내부 — 단일 검사 호출 + 예외 흡수
    # ------------------------------------------------------------------

    def _safe_invoke(
        self, fn: CheckFn, code: PreflightCheckCode
    ) -> CheckResult:
        """검사 함수를 호출하고 예외를 흡수해 CheckResult 로 변환.

        설계 원칙: 한 검사가 실패해도 다른 검사가 멈추면 안 됨. 예외 발생 시
        그 검사만 warning 으로 표시.
        """
        try:
            return fn(self.workspace, self.contract)
        except Exception as exc:  # noqa: BLE001 — 의도적 광범위
            log.warning("Check %s raised %s: %s", code.value, type(exc).__name__, exc)
            from schemas import PreflightSeverity, PreflightWarning
            return CheckResult(
                code=code,
                passed=False,
                duration_ms=0,
                warning=PreflightWarning(
                    code=code,
                    message=f"검사 실행 중 예외: {type(exc).__name__}",
                    fix_hint="ReCoder 로그를 확인하세요. 검사 자체의 버그일 수 있습니다.",
                    severity=PreflightSeverity.LOW,
                ),
                details={"exception": type(exc).__name__, "message": str(exc)[:200]},
            )

    # ------------------------------------------------------------------
    # 종합 — CheckResult 들 → PreflightRun
    # ------------------------------------------------------------------

    def _aggregate(
        self,
        results: list[CheckResult],
        registry: list[tuple[PreflightCheckCode, CheckFn]] = CHECK_REGISTRY,
    ) -> PreflightRun:
        """검사 결과들로 PreflightRun 구성."""
        # 결과 정렬 — CHECK_REGISTRY 순서대로 (decisive ordering for UI)
        order = {code: i for i, (code, _) in enumerate(registry)}
        results.sort(key=lambda r: order.get(r.code, 999))

        blockers = [r.blocker for r in results if r.blocker]
        warnings = [r.warning for r in results if r.warning]

        static_results = {r.code.value: r.to_details_dict() for r in results}

        return PreflightRun(
            project_id=self.project_id,
            contract_hash=self.contract.contract_hash,
            status=determine_status(results),
            score=compute_score(results),
            blockers=blockers,
            warnings=warnings,
            static_checks=PreflightStaticChecks(results=static_results),
        )


# ---------------------------------------------------------------------------
# 모듈 레벨 편의 함수
# ---------------------------------------------------------------------------


async def run_static_preflight(
    workspace_path: str,
    contract: ReleaseContract,
    project_id: Optional[str] = None,
) -> PreflightRun:
    """편의 async 함수. FastAPI 핸들러에서 직접 호출."""
    runner = StaticPreflightRunner(workspace_path, contract, project_id=project_id)
    return await runner.run_async()
