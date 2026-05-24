"""
ReCoder Core — Preflight 패키지 (설계서 §30 / v10).

Static Preflight (이 패키지) 와 Runtime Preflight (별도 모듈, B 영역) 가 함께
PreflightRun 을 채운다. 본 패키지는 Docker 를 띄우지 않는 정적 검사만 담당.

공개 API:
    from preflight import run_static_preflight, StaticPreflightRunner

    runner = StaticPreflightRunner(workspace_path, contract)
    result: PreflightRun = await runner.run_all()

각 검사는 ``CheckResult`` 를 반환하며, 12종 검사가 모두 끝나면
:class:`StaticPreflightRunner` 가 종합해서 ``PreflightRun`` 을 만든다.

검사별 모듈은 ``preflight.checks.*`` 에 분리되어 있어 단위 테스트가 쉽다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    from schemas import (
        PreflightBlocker,
        PreflightCheckCode,
        PreflightSeverity,
        PreflightWarning,
    )
except ImportError:  # pragma: no cover
    from core.schemas import (  # type: ignore
        PreflightBlocker,
        PreflightCheckCode,
        PreflightSeverity,
        PreflightWarning,
    )


# ---------------------------------------------------------------------------
# Common types
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """단일 검사의 결과.

    검사 함수가 반환하는 표준 구조. 결과를 받은 Runner 가 PreflightRun 의
    blockers / warnings / static_checks 영역에 적절히 분배한다.

    Attributes:
        code: PreflightCheckCode (어떤 검사였는지)
        passed: True 면 통과
        duration_ms: 검사 소요 시간
        blocker: 검사 실패 시 채워질 PreflightBlocker (배포 차단)
        warning: 검사 실패하지만 차단까지는 아닌 경우의 PreflightWarning
        details: 디버깅용 상세 (PreflightStaticChecks.results 에 저장)
    """

    code: PreflightCheckCode
    passed: bool
    duration_ms: int = 0
    blocker: Optional[PreflightBlocker] = None
    warning: Optional[PreflightWarning] = None
    details: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = {}

    def to_details_dict(self) -> dict[str, Any]:
        """PreflightStaticChecks.results[<code>] 로 저장될 dict."""
        return {
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Path safety helpers — Static Preflight 도 사용자 파일을 읽으므로 traversal 방지
# ---------------------------------------------------------------------------


def safe_workspace_path(workspace_path: str) -> Path:
    """workspace_path 를 정규화하고 절대경로로 변환.

    Raises:
        ValueError: 빈 문자열 / 존재하지 않는 경로 / 디렉토리 아님
    """
    if not workspace_path or not workspace_path.strip():
        raise ValueError("workspace_path 는 비어있을 수 없습니다.")
    p = Path(workspace_path).expanduser().resolve()
    if not p.exists():
        raise ValueError(f"workspace_path 가 존재하지 않습니다: {p}")
    if not p.is_dir():
        raise ValueError(f"workspace_path 는 디렉토리여야 합니다: {p}")
    return p


def safe_relative_join(workspace: Path, relative_path: str) -> Optional[Path]:
    """
    workspace 안의 상대 경로를 안전하게 결합. workspace 밖으로 빠지면 None 반환.

    Path traversal (``../etc/passwd``) 차단 — Preflight 가 사용자 입력을 받을 때
    필수.
    """
    if not relative_path:
        return None
    # 절대 경로면 차단
    if Path(relative_path).is_absolute():
        return None
    target = (workspace / relative_path).resolve()
    # workspace 가 target 의 부모인지 확인
    try:
        target.relative_to(workspace)
    except ValueError:
        return None
    return target


# ---------------------------------------------------------------------------
# Public API (re-export — 호출자가 짧게 import 하도록)
# ---------------------------------------------------------------------------

# 순환 import 회피 — 실제 import 는 호출 시점.
__all__ = [
    "CheckResult",
    "safe_workspace_path",
    "safe_relative_join",
    "run_static_preflight",
    "StaticPreflightRunner",
]


def __getattr__(name: str) -> Any:
    # Lazy re-export: preflight.static 로딩 시점을 지연 (테스트 시 부분 import 가능).
    if name in {"run_static_preflight", "StaticPreflightRunner"}:
        from .static import StaticPreflightRunner, run_static_preflight  # noqa: WPS433
        return {"StaticPreflightRunner": StaticPreflightRunner,
                "run_static_preflight": run_static_preflight}[name]
    raise AttributeError(name)
