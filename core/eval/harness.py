"""
ReCoder Q1 — Eval Harness

설계서 v5.0 §Eval Harness:

케이스 구축 전략:
- 수동으로 처음부터 만들지 않음
- 과거 실제 에러 / 오픈소스 이슈 / ReCoder 개발 중 마주친 에러 재활용
- Q1에는 카테고리별 뼈대 케이스 3~5개씩 총 20~30개

카테고리:
1. Python 단일 파일 에러 수정
2. Python 다중 파일 패치
3. Node.js 에러 수정 (line-based fallback)
4. Dockerfile 생성
5. docker build 실패
6. Health Check 실패

Gate:
- Internal: pass_rate >= 60%, Safety violation 0건
- Demo Release: pass_rate 100% (핵심 시나리오), Safety violation 0건
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from schemas import (
    EvalCase,
    EvalCategory,
    EvalReport,
    EvalResult,
    PatchProposal,
    SafetyViolationType,
)
from eval.safety import SafetyChecker

logger = logging.getLogger(__name__)

# Q1 gate threshold
_Q1_PASS_RATE_THRESHOLD = 0.60
_Q2_PASS_RATE_THRESHOLD = 0.80
_FALSE_POSITIVE_MAX = 0.05
_FALSE_NEGATIVE_MAX = 0.10


class EvalHarness:
    """
    Evaluation framework for ReCoder's AI pipeline.

    Usage::

        harness = EvalHarness(pipeline, cases_dir="core/eval/cases")
        report = await harness.run_all()
        assert report.ci_gate_passed, "CI gate failed"
    """

    def __init__(
        self,
        pipeline: Any,            # PlanExecuteVerifyPipeline instance
        cases_dir: Optional[str] = None,
        pass_rate_threshold: float = _Q1_PASS_RATE_THRESHOLD,
    ) -> None:
        self._pipeline = pipeline
        self._cases_dir = Path(cases_dir) if cases_dir else Path(__file__).parent / "cases"
        self._safety = SafetyChecker()
        self._threshold = pass_rate_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_all(self, tags: Optional[list[str]] = None) -> EvalReport:
        """Run all loaded cases and return an EvalReport."""
        cases = self.load_cases(tags=tags)
        if not cases:
            logger.warning("EvalHarness: no cases found in %s", self._cases_dir)
            return EvalReport(
                total=0, passed=0, failed=0, safety_violations=0,
                pass_rate=0.0, ci_gate_passed=False,
            )

        results: list[EvalResult] = []
        for case in cases:
            result = await self.run_case(case)
            results.append(result)
            status = "PASS" if result.passed else "FAIL"
            sv = f" [SAFETY:{','.join(v.value for v in result.safety_violations)}]" if result.safety_violations else ""
            logger.info("Eval [%s] %s %s%s", case.category.value, case.case_id, status, sv)

        return self._build_report(results)

    async def run_case(self, case: EvalCase) -> EvalResult:
        """Run a single EvalCase through the PEV pipeline."""
        import tempfile, os, shutil

        t0 = time.monotonic()
        ws = None

        try:
            # Create temporary workspace from snapshot
            ws = tempfile.mkdtemp(prefix="recoder_eval_")
            for rel_path, content in case.workspace_snapshot.items():
                fpath = Path(ws) / rel_path
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content, encoding="utf-8")

            from schemas import AnalyzeRequest
            request = AnalyzeRequest(
                workspace_path=ws,
                terminal_output=case.terminal_output,
                command=case.command,
            )

            pipeline_result = await self._pipeline.run(request)

            # Check safety
            violations: list[SafetyViolationType] = []
            proposal: Optional[PatchProposal] = pipeline_result.proposal
            if proposal:
                violations = self._safety.check(proposal, workspace_path=ws)

            # Evaluate pass criteria
            passed = self._evaluate_pass(case, pipeline_result, violations)

            return EvalResult(
                case_id=case.case_id,
                category=case.category,
                passed=passed,
                safety_violations=violations,
                proposal_summary=proposal.summary if proposal else None,
                patch_files=[p.file for p in proposal.patches] if proposal else [],
                duration_seconds=time.monotonic() - t0,
            )

        except Exception as exc:
            logger.exception("EvalCase %s raised exception: %s", case.case_id, exc)
            return EvalResult(
                case_id=case.case_id,
                category=case.category,
                passed=False,
                error_message=str(exc),
                duration_seconds=time.monotonic() - t0,
            )
        finally:
            if ws:
                try:
                    shutil.rmtree(ws)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Case loading
    # ------------------------------------------------------------------

    def load_cases(self, tags: Optional[list[str]] = None) -> list[EvalCase]:
        """
        Load EvalCase objects from JSON files in cases_dir.
        Optionally filter by tags.
        """
        cases: list[EvalCase] = []
        if not self._cases_dir.exists():
            logger.warning("EvalHarness: cases dir not found: %s", self._cases_dir)
            return cases

        for json_file in sorted(self._cases_dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                # Support both single case and list of cases
                if isinstance(data, list):
                    items = data
                else:
                    items = [data]

                for item in items:
                    case = EvalCase(**item)
                    if tags:
                        if not any(t in case.tags for t in tags):
                            continue
                    cases.append(case)
            except Exception as exc:
                logger.error("Failed to load eval case %s: %s", json_file, exc)

        return cases

    def save_report(self, report: EvalReport, output_path: str) -> None:
        """Persist an EvalReport to JSON."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        logger.info("Eval report saved to %s", output_path)

    # ------------------------------------------------------------------
    # Pass evaluation
    # ------------------------------------------------------------------

    def _evaluate_pass(
        self,
        case: EvalCase,
        pipeline_result: Any,
        violations: list[SafetyViolationType],
    ) -> bool:
        """
        A case passes when:
        1. No safety violations (if expected_no_safety_violations=True)
        2. A PatchProposal was produced (for code-fix categories)
        3. Expected files were touched (if specified)
        4. Expected keywords appear in summary or diffs (if specified)
        """
        # Safety gate — absolute block
        if case.expected_no_safety_violations and violations:
            return False

        proposal = pipeline_result.proposal

        # Categories that require a patch
        patch_required_categories = {
            EvalCategory.PYTHON_SINGLE_FILE,
            EvalCategory.PYTHON_MULTI_FILE,
            EvalCategory.NODEJS_ERROR,
            EvalCategory.DOCKER_BUILD_FAILURE,
        }

        if case.category in patch_required_categories:
            if proposal is None:
                return False

        # File check
        if case.expected_files_changed and proposal:
            patched = {p.file for p in proposal.patches}
            if not any(exp in str(patched) for exp in case.expected_files_changed):
                return False

        # Keyword check
        if case.expected_patch_keywords and proposal:
            full_text = (proposal.summary or "") + " ".join(
                p.unified_diff for p in proposal.patches
            )
            if not any(kw in full_text for kw in case.expected_patch_keywords):
                return False

        return True

    # ------------------------------------------------------------------
    # Report building
    # ------------------------------------------------------------------

    def _build_report(self, results: list[EvalResult]) -> EvalReport:
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        failed = total - passed
        safety_violations = sum(1 for r in results if r.has_safety_violation)
        pass_rate = passed / total if total > 0 else 0.0

        # Per-category breakdown
        by_category: dict[str, dict[str, Any]] = {}
        for cat in EvalCategory:
            cat_results = [r for r in results if r.category == cat]
            if not cat_results:
                continue
            cat_passed = sum(1 for r in cat_results if r.passed)
            by_category[cat.value] = {
                "total": len(cat_results),
                "passed": cat_passed,
                "pass_rate": cat_passed / len(cat_results),
                "safety_violations": sum(1 for r in cat_results if r.has_safety_violation),
            }

        # CI gate: safety_violations == 0 AND pass_rate >= threshold
        ci_gate_passed = (safety_violations == 0) and (pass_rate >= self._threshold)

        return EvalReport(
            total=total,
            passed=passed,
            failed=failed,
            safety_violations=safety_violations,
            pass_rate=pass_rate,
            by_category=by_category,
            results=results,
            ci_gate_passed=ci_gate_passed,
        )
