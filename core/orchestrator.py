"""
<<<<<<< HEAD
ReCoder Core — Orchestrator FSM

Finite State Machine that drives the full ReCoder analysis pipeline:
  IDLE -> COLLECTING_CONTEXT -> MASKING -> SCORING -> ANALYZING
       -> PROPOSING -> AWAITING_APPROVAL

Also handles patch application, rollback, infra / deploy / ops delegation.
=======
Orchestrator FSM (설계서 v6.4 §7) — 규칙 기반 상태 머신.

핵심 정책:
  - 외부 LLM 판단 제외 (Groq 등). 모든 전이는 명시적 규칙으로만.
  - DEPLOY_FAILED 무한 루프 방지 (§7.1) — 3회 실패 시 IDLE 복귀.
  - diff 적용 실패는 hash_mismatch / context_mismatch / unknown 으로 구분 (§7.2).
  - 전역 이벤트(USER_QUESTION/IGNORE/CANCEL/RESOLVED) 는 모든 상태에서 처리 가능.
  - v6.4 Stage 2 추가: SECURITY_SCANNING, DOCKER_BUILDING, HEALTH_CHECKING 상태.

설계서의 상태는 schemas.OrchestratorState에 정의되어 있다. 이 모듈은
허용 전이표(_VALID_TRANSITIONS)와 사이드이펙트 없는 transition() 함수를 제공한다.
>>>>>>> 74cf4369799da45d0fa49de67d56e58e01a2cc27
"""

from __future__ import annotations

<<<<<<< HEAD
import hashlib
import json
import logging
import shutil
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema imports (flexible: works from core/ or from project root)
# ---------------------------------------------------------------------------


def _schemas():
    try:
        from schemas import (
            AnalyzeRequest, PatchProposal, FilePatch,
            InfraFileProposal, DeploymentPlan, DeploymentRecord,
            AlertRecord, ResponseProposal, RiskLevel, ApprovalLevel,
        )
    except ImportError:
        from core.schemas import (
            AnalyzeRequest, PatchProposal, FilePatch,
            InfraFileProposal, DeploymentPlan, DeploymentRecord,
            AlertRecord, ResponseProposal, RiskLevel, ApprovalLevel,
        )
    return (
        AnalyzeRequest, PatchProposal, FilePatch,
        InfraFileProposal, DeploymentPlan, DeploymentRecord,
        AlertRecord, ResponseProposal, RiskLevel, ApprovalLevel,
    )


# ---------------------------------------------------------------------------
# FSM State Enum
# ---------------------------------------------------------------------------


class OrchestratorState(Enum):
    IDLE               = "idle"
    COLLECTING_CONTEXT = "collecting_context"
    MASKING            = "masking"
    SCORING            = "scoring"
    ANALYZING          = "analyzing"
    PROPOSING          = "proposing"
    AWAITING_APPROVAL  = "awaiting_approval"
    APPLYING           = "applying"
    ROLLING_BACK       = "rolling_back"
    COMPLETE           = "complete"
    ERROR              = "error"


# ---------------------------------------------------------------------------
# Code-analysis prompt helpers
# ---------------------------------------------------------------------------

_ANALYSIS_SYSTEM = (
    "You are an expert software engineer specialising in debugging and code repair. "
    "Analyse the provided error context and produce a minimal, safe patch proposal. "
    "Respond ONLY with valid JSON conforming to the supplied schema."
)

_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "patches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file":         {"type": "string"},
                    "unified_diff": {"type": "string"},
                    "reason":       {"type": "string"},
                },
                "required": ["file", "unified_diff", "reason"],
            },
        },
        "test_command": {"type": "string"},
        "risk_level":   {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "risk_reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "patches"],
}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """
    Central FSM controller.

    Depends on:
      - provider_router : LLMProviderRouter
      - context_gate    : ContextGate
    """

    _TRIGGER_THRESHOLD = 70.0    # below this → skip LLM
    _QUALITY_MIN       = 0.4     # below this → cancel AI call (ERROR)
    _QUALITY_WARN      = 0.7     # below this → request more context

    def __init__(self, provider_router: Any, context_gate: Any) -> None:
        self.state: OrchestratorState = OrchestratorState.IDLE
        self._router = provider_router
        self._gate   = context_gate
        self._session_id: str = str(uuid.uuid4())
        # proposal_id -> PatchProposal (for re-use on fingerprint cache hit)
        self._proposal_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Main analysis pipeline
    # ------------------------------------------------------------------

    async def process_analyze_request(self, request: Any) -> Optional[Any]:
        """
        Run the full analysis FSM.

        Returns a PatchProposal, or None when the request is filtered out.

        Raises ValueError with a descriptive message when the caller should
        surface a "need more context" message to the user.
        """
        (
            AnalyzeRequest, PatchProposal, FilePatch,
            InfraFileProposal, DeploymentPlan, DeploymentRecord,
            AlertRecord, ResponseProposal, RiskLevel, ApprovalLevel,
        ) = _schemas()

        try:
            # ---- IDLE -> COLLECTING_CONTEXT ---------------------------
            self._transition(OrchestratorState.COLLECTING_CONTEXT)
            raw_content = self._assemble_content(request)

            # ---- COLLECTING_CONTEXT -> MASKING ------------------------
            self._transition(OrchestratorState.MASKING)
            masking_result = await self._gate.mask(raw_content)
            masked_content = masking_result.masked_content

            # ---- MASKING -> SCORING -----------------------------------
            self._transition(OrchestratorState.SCORING)

            # Trigger score check
            trigger_score = self._gate.compute_trigger_score(
                request.terminal_output or ""
            )
            if trigger_score < self._TRIGGER_THRESHOLD:
                log.info(
                    "Trigger score %.1f < %.1f — skipping LLM call",
                    trigger_score, self._TRIGGER_THRESHOLD,
                )
                self._transition(OrchestratorState.IDLE)
                return None

            # Quality score check
            quality = self._gate.compute_quality_score(masked_content, request)
            if quality.score < self._QUALITY_MIN:
                self._transition(OrchestratorState.ERROR)
                log.warning("Quality score %.2f too low — cancelling AI call", quality.score)
                return None

            if quality.score < self._QUALITY_WARN:
                self._transition(OrchestratorState.ERROR)
                raise ValueError(
                    "The error context is incomplete. Please include the full "
                    "traceback and ensure the relevant source file is open in the editor."
                )

            # Fingerprint dedup
            fingerprint = self._gate.compute_error_fingerprint(masked_content, request)
            if self._gate.check_fingerprint_cache(fingerprint):
                cached = self._proposal_cache.get(fingerprint)
                if cached is not None:
                    log.info("Fingerprint cache hit — reusing existing proposal")
                    self._transition(OrchestratorState.AWAITING_APPROVAL)
                    return cached

            # ---- SCORING -> ANALYZING ---------------------------------
            self._transition(OrchestratorState.ANALYZING)
            prompt = self._build_analysis_prompt(masked_content, request)
            llm_result = await self._router.call_primary(prompt, schema=_ANALYSIS_SCHEMA)

            # ---- ANALYZING -> PROPOSING -------------------------------
            self._transition(OrchestratorState.PROPOSING)
            proposal = self._build_proposal(llm_result, RiskLevel, ApprovalLevel,
                                            PatchProposal, FilePatch)

            # Cache and move to AWAITING_APPROVAL
            self._gate.update_fingerprint_cache(fingerprint)
            self._proposal_cache[fingerprint] = proposal

            self._transition(OrchestratorState.AWAITING_APPROVAL)
            return proposal

        except ValueError:
            raise
        except Exception as exc:
            log.exception("Orchestrator pipeline error: %s", exc)
            self._transition(OrchestratorState.ERROR)
            return None

    # ------------------------------------------------------------------
    # Patch application & rollback
    # ------------------------------------------------------------------

    async def apply_patch(self, proposal: Any) -> bool:
        """
        Apply a PatchProposal to the filesystem.

        Steps
        -----
        1. Verify each file's base_sha256 (if provided).
        2. Backup affected files to ~/.recoder/backups/{session_id}/
        3. Apply unified diffs via stdlib difflib / patch command.
        4. On any failure: auto-rollback all applied patches.

        Returns True on success, False on failure.
        """
        self._transition(OrchestratorState.APPLYING)

        applied: list[str] = []
        try:
            for patch in proposal.patches:
                file_path = Path(patch.file)

                # Integrity check
                if patch.base_sha256 and file_path.exists():
                    actual = hashlib.sha256(
                        file_path.read_bytes()
                    ).hexdigest()
                    if actual != patch.base_sha256:
                        raise ValueError(
                            f"SHA-256 mismatch for {patch.file}: "
                            f"expected {patch.base_sha256}, got {actual}"
                        )

                # Backup
                self._backup_file(self._session_id, str(file_path))

                # Apply diff
                self._apply_unified_diff(file_path, patch.unified_diff)
                applied.append(str(file_path))

            self._transition(OrchestratorState.COMPLETE)
            return True

        except Exception as exc:
            log.error("Patch application failed: %s — rolling back", exc)
            self._transition(OrchestratorState.ROLLING_BACK)
            for fp in applied:
                try:
                    self._rollback_file(self._session_id, fp)
                except Exception as rb_exc:
                    log.error("Rollback failed for %s: %s", fp, rb_exc)
            self._transition(OrchestratorState.ERROR)
            return False

    # ------------------------------------------------------------------
    # Delegation to other agents (stubs that sub-agents will implement)
    # ------------------------------------------------------------------

    async def process_infra_request(
        self, workspace_path: str, project_id: str
    ) -> Any:
        """Delegate to InfraAgent (imported lazily to avoid circular deps)."""
        try:
            from agents.infra_agent import InfraAgent  # type: ignore
        except ImportError:
            try:
                from core.agents.infra_agent import InfraAgent  # type: ignore
            except ImportError:
                raise NotImplementedError("InfraAgent not yet implemented")
        agent = InfraAgent(self._router)
        return await agent.generate(workspace_path, project_id)

    async def process_deploy_request(self, plan: Any) -> Any:
        """Delegate to DeployAgent."""
        try:
            from agents.deploy_agent import DeployAgent  # type: ignore
        except ImportError:
            try:
                from core.agents.deploy_agent import DeployAgent  # type: ignore
            except ImportError:
                raise NotImplementedError("DeployAgent not yet implemented")
        agent = DeployAgent(self._router)
        return await agent.execute(plan)

    async def process_ops_request(self, alert: Any) -> Any:
        """Delegate to OpsAgent."""
        try:
            from agents.ops_agent import OpsAgent  # type: ignore
        except ImportError:
            try:
                from core.agents.ops_agent import OpsAgent  # type: ignore
            except ImportError:
                raise NotImplementedError("OpsAgent not yet implemented")
        agent = OpsAgent(self._router)
        return await agent.diagnose(alert)

    # ------------------------------------------------------------------
    # Backup / rollback helpers
    # ------------------------------------------------------------------

    def _backup_file(self, session_id: str, file_path: str) -> Path:
        """
        Copy *file_path* to ~/.recoder/backups/{session_id}/{relative_path}.

        Returns the backup destination path.
        """
        src = Path(file_path)
        if not src.exists():
            # Nothing to backup (new file creation)
            return Path(file_path)

        # Build destination preserving original structure (strip leading separators)
        rel = Path(file_path.lstrip("/\\"))
        dest = Path.home() / ".recoder" / "backups" / session_id / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        log.debug("Backed up %s -> %s", file_path, dest)
        return dest

    def _rollback_file(self, session_id: str, file_path: str) -> bool:
        """
        Restore *file_path* from its backup.

        Returns True on success, False when no backup exists.
        """
        rel = Path(file_path.lstrip("/\\"))
        backup = Path.home() / ".recoder" / "backups" / session_id / rel
        if not backup.exists():
            log.warning("No backup found for %s", file_path)
            return False
        dest = Path(file_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, dest)
        log.info("Rolled back %s from backup", file_path)
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _transition(self, new_state: OrchestratorState) -> None:
        log.debug("FSM: %s -> %s", self.state.value, new_state.value)
        self.state = new_state

    @staticmethod
    def _assemble_content(request: Any) -> str:
        """Concatenate all relevant request fields into a single string."""
        parts: list[str] = []
        if request.terminal_output:
            parts.append(f"[TERMINAL OUTPUT]\n{request.terminal_output}")
        if request.selected_text:
            parts.append(f"[SELECTED TEXT]\n{request.selected_text}")
        if request.active_file_path:
            parts.append(f"[ACTIVE FILE]\n{request.active_file_path}")
        if request.project_files_summary:
            parts.append(f"[PROJECT FILES SUMMARY]\n{request.project_files_summary}")
        if request.command:
            parts.append(f"[COMMAND]\n{request.command}")
        return "\n\n".join(parts)

    @staticmethod
    def _build_analysis_prompt(masked_content: str, request: Any) -> str:
        return (
            f"Workspace: {request.workspace_path}\n\n"
            f"{masked_content}\n\n"
            "Analyse the error above and propose minimal, safe file patches."
        )

    @staticmethod
    def _build_proposal(
        llm_result: dict,
        RiskLevel: Any,
        ApprovalLevel: Any,
        PatchProposal: Any,
        FilePatch: Any,
    ) -> Any:
        """Convert raw LLM JSON output into a validated PatchProposal."""
        raw_patches = llm_result.get("patches", [])
        patches: list[Any] = []
        for p in raw_patches:
            patches.append(FilePatch(
                file=p.get("file", ""),
                base_sha256=p.get("base_sha256"),
                unified_diff=p.get("unified_diff", ""),
                reason=p.get("reason", ""),
            ))

        # Parse risk level safely
        raw_risk = llm_result.get("risk_level", "low")
        try:
            risk = RiskLevel(raw_risk)
        except ValueError:
            risk = RiskLevel.MEDIUM

        # Determine approval level based on risk
        approval_map = {
            RiskLevel.LOW:      ApprovalLevel.AUTO,
            RiskLevel.MEDIUM:   ApprovalLevel.CONFIRM,
            RiskLevel.HIGH:     ApprovalLevel.DOUBLE_CONFIRM,
            RiskLevel.CRITICAL: ApprovalLevel.BLOCKED,
        }
        approval = approval_map.get(risk, ApprovalLevel.CONFIRM)

        return PatchProposal(
            summary=llm_result.get("summary", "AI-generated patch"),
            risk_level=risk,
            risk_reasons=llm_result.get("risk_reasons", []),
            approval_level=approval,
            patches=patches,
            test_command=llm_result.get("test_command"),
        )

    @staticmethod
    def _apply_unified_diff(file_path: Path, unified_diff: str) -> None:
        """
        Apply a unified diff string to *file_path*.

        Uses Python's ``patch`` library when available, otherwise falls back
        to the system ``patch`` command.  New files are created if the diff
        targets /dev/null.
        """
        import subprocess
        import tempfile
        import os

        if not unified_diff.strip():
            log.debug("Empty diff for %s — skipping", file_path)
            return

        # Write diff to a temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(unified_diff)
            tmp_path = tmp.name

        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["patch", "-p1", "--forward", str(file_path)],
                input=unified_diff,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"patch command failed for {file_path}: {result.stderr}"
                )
        finally:
            os.unlink(tmp_path)
=======
from typing import Optional

from schemas import OrchestratorState as S


class IllegalTransition(Exception):
    """허용되지 않은 상태 전이 시도."""

    def __init__(self, src: S, event: str, dst: Optional[S] = None) -> None:
        self.src = src
        self.event = event
        self.dst = dst
        super().__init__(f"illegal transition: {src.value} --{event}--> {dst.value if dst else '?'}")


# ── 전이표 ────────────────────────────────────────────────────────────
# (src, event) -> dst
# event는 추상 명령 — 모니터/에이전트가 이 이벤트를 발생시키면 FSM이 새 상태를 반환.
_VALID_TRANSITIONS: dict[tuple[S, str], S] = {
    # IDLE
    (S.IDLE,                "ERROR_FOUND"):              S.ERROR_DETECTED,
    (S.IDLE,                "TASK_CHANGED"):             S.IDLE,
    (S.IDLE,                "USER_QUESTION"):            S.ANALYZING,

    # ERROR_DETECTED
    (S.ERROR_DETECTED,      "USER_REQUEST_FIX"):         S.ANALYZING,
    (S.ERROR_DETECTED,      "USER_REQUEST_EXPLAIN"):     S.ANALYZING,
    (S.ERROR_DETECTED,      "USER_REQUEST_INFRA"):       S.ANALYZING,
    (S.ERROR_DETECTED,      "USER_REQUEST_DEPLOY"):      S.ANALYZING,
    (S.ERROR_DETECTED,      "USER_IGNORE"):              S.IDLE,
    (S.ERROR_DETECTED,      "RESOLVED"):                 S.IDLE,

    # ANALYZING
    (S.ANALYZING,           "PATCH_PROPOSED"):           S.CODE_PATCH_PROPOSED,
    (S.ANALYZING,           "INFRA_PROPOSED"):           S.INFRA_PROPOSED,
    (S.ANALYZING,           "DEPLOY_PROPOSED"):          S.DEPLOY_PROPOSED,
    (S.ANALYZING,           "WAIT_USER_INPUT"):          S.WAITING_USER_ACTION,
    (S.ANALYZING,           "ANALYSIS_FAILED"):          S.IDLE,
    (S.ANALYZING,           "USER_IGNORE"):              S.IDLE,

    # WAITING_USER_ACTION (설계서 v6.4 §7.1 — fix_code / infra / deploy / explain / ignore)
    (S.WAITING_USER_ACTION, "USER_REQUEST_FIX"):         S.ANALYZING,
    (S.WAITING_USER_ACTION, "USER_REQUEST_EXPLAIN"):     S.ANALYZING,
    (S.WAITING_USER_ACTION, "USER_REQUEST_INFRA"):       S.ANALYZING,
    (S.WAITING_USER_ACTION, "USER_REQUEST_DEPLOY"):      S.ANALYZING,
    (S.WAITING_USER_ACTION, "USER_IGNORE"):              S.IDLE,
    (S.WAITING_USER_ACTION, "RESOLVED"):                 S.IDLE,

    # CODE_PATCH_PROPOSED
    (S.CODE_PATCH_PROPOSED, "USER_APPROVE_PATCH"):       S.APPLYING_PATCH,
    (S.CODE_PATCH_PROPOSED, "USER_REJECT_PATCH"):        S.IDLE,
    (S.CODE_PATCH_PROPOSED, "USER_IGNORE"):              S.IDLE,

    # APPLYING_PATCH
    (S.APPLYING_PATCH,      "PATCH_APPLIED"):            S.CODE_READY,
    (S.APPLYING_PATCH,      "PATCH_HASH_MISMATCH"):      S.ANALYZING,   # 재분석
    (S.APPLYING_PATCH,      "PATCH_CONTEXT_MISMATCH"):   S.CODE_PATCH_PROPOSED,  # 사용자가 수동 편집 후 재시도
    (S.APPLYING_PATCH,      "PATCH_UNKNOWN_ERROR"):      S.ROLLBACK,

    # CODE_READY
    (S.CODE_READY,          "USER_REQUEST_INFRA"):       S.ANALYZING,
    (S.CODE_READY,          "USER_REQUEST_DEPLOY"):      S.DEPLOY_PROPOSED,
    (S.CODE_READY,          "USER_IGNORE"):              S.IDLE,
    (S.CODE_READY,          "RESOLVED"):                 S.IDLE,

    # INFRA_PROPOSED
    (S.INFRA_PROPOSED,      "USER_APPROVE_INFRA"):       S.INFRA_READY,
    (S.INFRA_PROPOSED,      "USER_REJECT_INFRA"):        S.IDLE,

    # INFRA_READY (Stage 2 security scanning 추가)
    (S.INFRA_READY,         "START_SECURITY_SCAN"):      S.SECURITY_SCANNING,
    (S.INFRA_READY,         "USER_REQUEST_DEPLOY"):      S.DEPLOY_PROPOSED,
    (S.INFRA_READY,         "USER_IGNORE"):              S.IDLE,

    # SECURITY_SCANNING (v6.4 신규: Trivy/Hadolint)
    (S.SECURITY_SCANNING,   "SCAN_PASSED"):              S.INFRA_READY,
    (S.SECURITY_SCANNING,   "SCAN_FAILED"):              S.WAITING_USER_ACTION,

    # DEPLOY_PROPOSED (Stage 2 진입점)
    (S.DEPLOY_PROPOSED,     "USER_APPROVE_DEPLOY"):      S.DOCKER_BUILDING,
    (S.DEPLOY_PROPOSED,     "USER_REJECT_DEPLOY"):       S.IDLE,

    # DOCKER_BUILDING (v6.4 신규: docker build)
    (S.DOCKER_BUILDING,     "BUILD_SUCCESS"):            S.HEALTH_CHECKING,
    (S.DOCKER_BUILDING,     "BUILD_FAILED"):             S.DEPLOY_FAILED,

    # HEALTH_CHECKING (v6.4 신규: Health Check)
    (S.HEALTH_CHECKING,     "HEALTH_OK"):                S.DEPLOYED,
    (S.HEALTH_CHECKING,     "HEALTH_FAILED"):            S.ROLLBACK,

    # DEPLOY_FAILED
    (S.DEPLOY_FAILED,       "RETRY"):                    S.ANALYZING,   # 새 해결책 생성
    (S.DEPLOY_FAILED,       "GIVE_UP"):                  S.IDLE,
    (S.DEPLOY_FAILED,       "USER_IGNORE"):              S.IDLE,

    # DEPLOYED
    (S.DEPLOYED,            "RESOLVED"):                 S.IDLE,
    (S.DEPLOYED,            "ROLLBACK_REQUESTED"):       S.ROLLBACK,

    # ROLLBACK
    (S.ROLLBACK,            "ROLLBACK_OK"):              S.IDLE,
    (S.ROLLBACK,            "ROLLBACK_FAILED"):          S.IDLE,
}


# 전역 이벤트 — 모든 상태에서 IDLE로 갈 수 있는 escape hatch들 (§7.3)
_GLOBAL_EVENTS = {"USER_CANCEL", "GLOBAL_RESET"}


def transition(src: S, event: str) -> S:
    """
    설계서 §7 — 상태 전이.
    허용되지 않은 전이는 IllegalTransition 예외.
    USER_CANCEL/GLOBAL_RESET 은 어느 상태에서나 IDLE로 갈 수 있다 (§7.3).
    """
    if event in _GLOBAL_EVENTS:
        return S.IDLE
    key = (src, event)
    if key not in _VALID_TRANSITIONS:
        raise IllegalTransition(src, event)
    return _VALID_TRANSITIONS[key]


def is_terminal(state: S) -> bool:
    """완료/대기 상태인지 — 위젯이 다음 입력을 받을 준비가 됐는지 판단."""
    return state in {S.IDLE, S.CODE_READY, S.DEPLOYED, S.WAITING_USER_ACTION}


# ── DEPLOY_FAILED 무한 루프 가드 (§7.1) ───────────────────────────────
# Deploy Agent가 자체 카운터를 가지고 있지만, FSM 레벨에서도 한 번 더 보호.

_MAX_DEPLOY_RETRIES = 3
_deploy_retry_counter: dict[str, int] = {}


def record_deploy_failure(plan_id: str) -> bool:
    """
    실패 기록. 임계값 도달 시 True 반환 (이후 RETRY는 거부해야 함).
    """
    if not plan_id:
        return False
    _deploy_retry_counter[plan_id] = _deploy_retry_counter.get(plan_id, 0) + 1
    return _deploy_retry_counter[plan_id] >= _MAX_DEPLOY_RETRIES


def can_retry_deploy(plan_id: str) -> bool:
    return _deploy_retry_counter.get(plan_id, 0) < _MAX_DEPLOY_RETRIES


def reset_deploy_retries(plan_id: str = "") -> None:
    if not plan_id:
        _deploy_retry_counter.clear()
        return
    _deploy_retry_counter.pop(plan_id, None)
>>>>>>> 74cf4369799da45d0fa49de67d56e58e01a2cc27
