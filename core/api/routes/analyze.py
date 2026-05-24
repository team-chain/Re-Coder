"""
ReCoder Core — Code Analysis Routes

Handles LLM-powered patch proposal generation, approval, and listing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from schemas import AnalyzeRequest, ApprovalLevel, FilePatch, PatchProposal, RiskLevel

router = APIRouter(tags=["analyze"])

# ---------------------------------------------------------------------------
# In-process stores (per server lifetime)
# ---------------------------------------------------------------------------

# proposal_id -> PatchProposal
_proposals: dict[str, PatchProposal] = {}

# fingerprint -> proposal_id (§19.1 4-stage filter, step 3: dedup cache)
# Keyed by error fingerprint; values point into _proposals. Lifetime is
# bounded by the ContextGate's 60-second TTL check — we don't garbage-collect
# this map explicitly because cache misses simply create a fresh proposal.
_fingerprint_to_proposal: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Lazy singletons — optional deps don't block server startup
# ---------------------------------------------------------------------------

_orchestrator = None   # type: ignore
_gate = None           # type: ignore  (ContextGate)


def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        try:
            from llm.provider_router import LLMProviderRouter  # type: ignore
            from context_gate import ContextGate  # type: ignore
            from orchestrator import Orchestrator  # type: ignore
            _orchestrator = Orchestrator(
                provider_router=LLMProviderRouter(),
                context_gate=_get_gate(),
            )
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("Orchestrator unavailable: %s", exc)
    return _orchestrator


def _get_gate():
    """Return the shared ContextGate singleton."""
    global _gate
    if _gate is None:
        try:
            from context_gate import ContextGate  # type: ignore
            _gate = ContextGate()
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("ContextGate unavailable: %s", exc)
    return _gate


# ---------------------------------------------------------------------------
# Context masking helpers
# ---------------------------------------------------------------------------


async def _mask_request(request: AnalyzeRequest) -> AnalyzeRequest:
    """
    Run all context fields through the ContextGate masker.

    Returns a copy of the request with PII / secrets redacted.
    Falls back to a simple regex-based scrub when ContextGate is unavailable.
    """
    gate = _get_gate()
    if gate is not None:
        # Mask each text field independently
        fields: dict[str, Optional[str]] = {
            "terminal_output": request.terminal_output,
            "selected_text": request.selected_text,
            "project_files_summary": request.project_files_summary,
        }
        masked: dict[str, Optional[str]] = {}
        for key, value in fields.items():
            if value is not None:
                result = await gate.mask(value)
                masked[key] = result.masked_content
            else:
                masked[key] = None
        return request.model_copy(update=masked)

    # Fallback minimal masking when ContextGate is absent
    import re
    _SECRET_RE = re.compile(
        r"(AKIA[0-9A-Z]{16}|(?:password|secret|token|key)\s*=\s*\S+)",
        re.IGNORECASE,
    )

    def _redact(text: Optional[str]) -> Optional[str]:
        return _SECRET_RE.sub("[REDACTED]", text) if text else text

    return request.model_copy(update={
        "terminal_output": _redact(request.terminal_output),
        "selected_text": _redact(request.selected_text),
        "project_files_summary": _redact(request.project_files_summary),
    })


def _compute_trigger_score(request: AnalyzeRequest) -> float:
    """
    Use the ContextGate's trigger scorer when available;
    fall back to a simple rule-based heuristic in [0.0, 1.0].
    """
    gate = _get_gate()
    if gate is not None and request.terminal_output:
        # ContextGate returns score in [0, 100]; normalise to [0, 1]
        return gate.compute_trigger_score(request.terminal_output) / 100.0

    # Inline fallback
    score = 0.0
    if request.terminal_output:
        output = request.terminal_output.lower()
        if "traceback" in output or "error" in output:
            score += 0.4
        if "exception" in output:
            score += 0.2
        if "warning" in output:
            score += 0.1
    if request.selected_text:
        score += 0.1
    if request.command:
        score += 0.1
    if request.active_file_path:
        score += 0.1
    return min(score, 1.0)


def _compute_quality_score(masked_request: AnalyzeRequest) -> float:
    """
    Use the ContextGate's quality scorer when available;
    fall back to a simple heuristic in [0.0, 1.0].
    """
    gate = _get_gate()
    if gate is not None:
        combined = " ".join(filter(None, [
            masked_request.terminal_output,
            masked_request.selected_text,
            masked_request.project_files_summary,
        ]))
        qs = gate.compute_quality_score(combined, masked_request)
        return qs.score

    # Inline fallback
    score = 0.0
    if masked_request.terminal_output and len(masked_request.terminal_output) > 50:
        score += 0.3
    if masked_request.selected_text and len(masked_request.selected_text) > 10:
        score += 0.2
    if masked_request.project_files_summary:
        score += 0.2
    if masked_request.workspace_path:
        score += 0.15
    if masked_request.active_file_path:
        score += 0.15
    return min(score, 1.0)


def _get_fingerprint(masked_request: AnalyzeRequest) -> str:
    """
    Use the ContextGate's error fingerprinting when available;
    fall back to SHA-256 of key fields.
    """
    gate = _get_gate()
    combined = " ".join(filter(None, [
        masked_request.terminal_output,
        masked_request.selected_text,
    ]))
    if gate is not None:
        return gate.compute_error_fingerprint(combined, masked_request)

    raw = (
        (masked_request.terminal_output or "")
        + (masked_request.active_file_path or "")
        + (masked_request.selected_text or "")
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _check_fingerprint_cache(fingerprint: str) -> bool:
    """Check the ContextGate's 60-second dedup cache."""
    gate = _get_gate()
    if gate is not None:
        return gate.check_fingerprint_cache(fingerprint)
    return False


def _update_fingerprint_cache(fingerprint: str) -> None:
    gate = _get_gate()
    if gate is not None:
        gate.update_fingerprint_cache(fingerprint)


# ---------------------------------------------------------------------------
# Orchestrator delegation
# ---------------------------------------------------------------------------


async def _delegate_to_orchestrator(
    request: AnalyzeRequest,
    trigger_score: float,
    quality_score: float,
) -> PatchProposal:
    """
    Delegate analysis to the Orchestrator (Code Agent) via process_analyze_request().

    Falls back to a structured placeholder when:
      - Orchestrator module is unavailable
      - Trigger/quality score is below the lower threshold (returns None)
      - Quality score is in the WARN band (raises ValueError per §18.3 —
        "사용자에게 추가 정보 요청"). We translate that to a friendly placeholder
        so the HTTP endpoint never returns a raw 500.
    """
    orch = _get_orchestrator()
    if orch is not None:
        try:
            result = await orch.process_analyze_request(request)
        except ValueError as exc:
            # §18.3 quality WARN band — orchestrator asks for richer context.
            # Convert to a structured "needs more context" PatchProposal so the
            # caller (Extension) can prompt the user to add traceback / file etc.
            import logging
            logging.getLogger(__name__).info(
                "[Analyze] Orchestrator requested more context: %s", exc,
            )
            return PatchProposal(
                summary="추가 컨텍스트가 필요합니다.",
                risk_level=RiskLevel.LOW,
                risk_reasons=[
                    "Quality score in WARN band — more context required.",
                    str(exc),
                ],
                approval_level=ApprovalLevel.AUTO,
                patches=[],
                test_command=None,
            )

        if result is not None:
            return result

        # Orchestrator filtered the request (low trigger/quality score)
        return PatchProposal(
            summary="No significant issues detected in the current context.",
            risk_level=RiskLevel.LOW,
            risk_reasons=["Trigger or quality score below threshold — no LLM call made."],
            approval_level=ApprovalLevel.AUTO,
            patches=[],
            test_command=None,
        )

    return PatchProposal(
        summary="[Placeholder] Orchestrator not yet available.",
        risk_level=RiskLevel.LOW,
        risk_reasons=["Orchestrator module not loaded — placeholder response."],
        approval_level=ApprovalLevel.AUTO,
        patches=[],
        test_command=None,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/analyze")
async def analyze(request: AnalyzeRequest) -> PatchProposal:
    """
    Analyse the provided workspace context and return a PatchProposal.

    Steps:
    1. Mask secrets / PII via the Context Gate (async, thread-pooled).
    2. Compute rule-based trigger and quality scores via ContextGate.
    3. Check the 60-second error fingerprint dedup cache.
    4. Delegate to the Orchestrator (Code Agent).
    5. Store the proposal for later approval.
    """
    # 1. Masking (async — offloads CPU work to thread pool via ContextGate)
    masked_request = await _mask_request(request)

    # 2. Scores via ContextGate methods (§19.1 stages 1 & 2)
    trigger_score = _compute_trigger_score(masked_request)
    quality_score = _compute_quality_score(masked_request)

    # 3. Fingerprint dedup via ContextGate 60-second cache (§19.1 stage 3)
    fingerprint = _get_fingerprint(masked_request)
    if _check_fingerprint_cache(fingerprint):
        cached_id = _fingerprint_to_proposal.get(fingerprint)
        if cached_id is not None:
            cached = _proposals.get(cached_id)
            if cached is not None:
                # Same error within 60s — return the prior proposal verbatim,
                # avoiding a duplicate LLM call. (설계서 §19.1 단계 3)
                import logging
                logging.getLogger(__name__).info(
                    "[FingerprintCache] hit fingerprint=%s proposal_id=%s — skipping LLM",
                    fingerprint[:12], cached_id,
                )
                return cached
        # Cache flag was set but no proposal stored — fall through (treat as miss)

    # 4. Delegate to Orchestrator (stages 1+2 thresholds applied internally;
    #    stage 4 AST chunking happens inside the orchestrator's LLM prompt build)
    proposal = await _delegate_to_orchestrator(masked_request, trigger_score, quality_score)

    # 5. Store and update both caches
    _proposals[proposal.proposal_id] = proposal
    _fingerprint_to_proposal[fingerprint] = proposal.proposal_id
    _update_fingerprint_cache(fingerprint)

    return proposal


@router.post("/api/analyze/approve")
async def approve_patch(proposal_id: str, approved: bool) -> dict:
    """
    Approve or reject a PatchProposal.

    On approval:
    - Validate base_sha256 for each patch.
    - Create backups of affected files.
    - Apply unified diffs.
    - Roll back on any failure.

    On rejection: simply removes the proposal from the store.
    """
    proposal = _proposals.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Proposal '{proposal_id}' not found.")

    if not approved:
        del _proposals[proposal_id]
        return {"status": "rejected", "proposal_id": proposal_id}

    applied: list[str] = []
    backups: dict[str, bytes] = {}

    try:
        for patch in proposal.patches:
            file_path = Path(patch.file)
            if not file_path.is_absolute():
                raise HTTPException(
                    status_code=422,
                    detail=f"Patch file path must be absolute: '{patch.file}'",
                )

            # Validate base_sha256
            if file_path.exists() and patch.base_sha256:
                current_digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
                if current_digest != patch.base_sha256:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"File '{patch.file}' has been modified since the proposal "
                            f"was generated (SHA-256 mismatch)."
                        ),
                    )
                backups[patch.file] = file_path.read_bytes()

            # Apply the unified diff
            _apply_unified_diff(file_path, patch)
            applied.append(patch.file)

    except HTTPException:
        # Rollback already-applied patches
        for fpath_str, original_bytes in backups.items():
            try:
                Path(fpath_str).write_bytes(original_bytes)
            except Exception:
                pass
        raise
    except Exception as exc:
        # Rollback on unexpected errors
        for fpath_str, original_bytes in backups.items():
            try:
                Path(fpath_str).write_bytes(original_bytes)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Patch application failed: {exc}") from exc

    del _proposals[proposal_id]
    return {"status": "applied", "proposal_id": proposal_id, "applied_files": applied}


@router.get("/api/analyze/proposals")
async def list_proposals() -> list[PatchProposal]:
    """Return all pending (unapproved) PatchProposals in the current session."""
    return list(_proposals.values())


# ---------------------------------------------------------------------------
# Diff application helper
# ---------------------------------------------------------------------------


def _apply_unified_diff(file_path: Path, patch: FilePatch) -> None:
    """
    Apply a unified diff to *file_path*.

    Uses the ``whatthepatch`` library when available, falling back to writing
    the full content if the diff is not in standard unified format.
    """
    try:
        import whatthepatch  # type: ignore

        original_text = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
        patches = list(whatthepatch.parse_patch(patch.unified_diff))
        if not patches:
            raise ValueError("No patches parsed from unified diff.")
        new_text = whatthepatch.apply_diff(patches[0], original_text)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(new_text or "", encoding="utf-8")

    except ImportError:
        # Fallback: write the diff as-is (dev/testing convenience)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(patch.unified_diff, encoding="utf-8")
