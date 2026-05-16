"""
ReCoder Core — Orchestrator FSM

Finite State Machine that drives the full ReCoder analysis pipeline:
  IDLE -> COLLECTING_CONTEXT -> MASKING -> SCORING -> ANALYZING
       -> PROPOSING -> AWAITING_APPROVAL

Also handles patch application, rollback, infra / deploy / ops delegation.
"""

from __future__ import annotations

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
