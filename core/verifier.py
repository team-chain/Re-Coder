"""
ReCoder Q1 — VerifierAgent

설계서 v5.0 §Plan-Execute-Verify:

VerifierAgent (LLM 없음):
- Schema validation
- base_sha256 검증
- test_command 실행
- 재시도 최대 2회
- 소진 시 Approval UI에 "자동 검증 실패, 수동 검토 필요" 표시 후 멈춤
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Any, Optional

from schemas import (
    PatchProposal,
    VerificationResult,
)

logger = logging.getLogger(__name__)

# 설계서: test_command 타임아웃 30초 필수
_TEST_TIMEOUT = 30

# Shell metacharacter 차단
_SHELL_META = set(';|&`$><(){}[]\\!')

# test_command allowlist prefixes
_ALLOWED_PREFIXES = (
    "npm ", "yarn ", "pnpm ", "npx ",
    "python ", "python3 ", "pytest", "py.test",
    "poetry run ", "uv run ",
)


class VerifierAgent:
    """
    Deterministic verifier — no LLM calls.

    Checks:
    1. PatchProposal schema validity (Pydantic already enforces this)
    2. base_sha256 integrity for each patched file
    3. test_command execution (if supplied)

    Retry policy: Execute → Verify loop 최대 2회.
    Exhausted → needs_manual_review = True.
    """

    _MAX_RETRIES = 2

    async def verify(
        self,
        proposal: PatchProposal,
        plan_id: str,
        workspace_path: str,
        retry_count: int = 0,
    ) -> VerificationResult:
        """
        Run all verification checks on a PatchProposal.

        Returns a VerificationResult.
        needs_manual_review is True when retry_count >= _MAX_RETRIES.
        """
        result = VerificationResult(
            plan_id=plan_id,
            proposal_id=proposal.proposal_id,
            retry_count=retry_count,
        )

        # 1. Schema check (implicit — proposal was already parsed by Pydantic)
        result.schema_valid = True
        if not proposal.patches:
            result.schema_valid = False
            logger.warning("VerifierAgent: proposal %s has no patches", proposal.proposal_id)

        # 2. SHA-256 integrity
        result.sha256_valid = self._verify_sha256(proposal, workspace_path)

        # 3. test_command
        if proposal.test_command:
            passed, output = await self._run_test_command(
                proposal.test_command, workspace_path
            )
            result.test_passed = passed
            result.test_output = output
        else:
            result.test_passed = None  # No test supplied

        # Determine if manual review is needed
        all_passed = (
            result.schema_valid
            and result.sha256_valid
            and (result.test_passed is None or result.test_passed)
        )

        if not all_passed and retry_count >= self._MAX_RETRIES:
            result.needs_manual_review = True
            logger.warning(
                "VerifierAgent: retry limit reached for proposal %s — manual review required",
                proposal.proposal_id,
            )

        return result

    # ------------------------------------------------------------------
    # SHA-256 verification
    # ------------------------------------------------------------------

    def _verify_sha256(self, proposal: PatchProposal, workspace_path: str) -> bool:
        """
        Check that each patch's base_sha256 matches the current file on disk.

        If base_sha256 is None, skip check for that file (new file creation).
        Returns True if ALL checks pass.
        """
        ws = Path(workspace_path)
        for patch in proposal.patches:
            if not patch.base_sha256:
                continue  # New file — no integrity check needed

            # Resolve to absolute path
            fpath = Path(patch.file)
            if not fpath.is_absolute():
                fpath = ws / patch.file

            if not fpath.exists():
                logger.warning(
                    "VerifierAgent: file %s not found for sha256 check", patch.file
                )
                return False

            try:
                actual = hashlib.sha256(fpath.read_bytes()).hexdigest()
            except OSError as exc:
                logger.error("VerifierAgent: cannot read %s: %s", fpath, exc)
                return False

            if actual != patch.base_sha256:
                logger.warning(
                    "VerifierAgent: SHA-256 mismatch for %s (expected %s, got %s)",
                    patch.file,
                    patch.base_sha256[:12],
                    actual[:12],
                )
                return False

        return True

    # ------------------------------------------------------------------
    # test_command execution
    # ------------------------------------------------------------------

    async def _run_test_command(
        self, command: str, workspace_path: str
    ) -> tuple[bool, str]:
        """
        Execute test_command with safety constraints.

        Returns (passed: bool, output: str).
        """
        # Shell metacharacter check
        if any(ch in command for ch in _SHELL_META):
            msg = f"test_command blocked: shell metacharacter in {command!r}"
            logger.error("VerifierAgent: %s", msg)
            return False, msg

        # Allowlist check
        if not any(command.startswith(p) for p in _ALLOWED_PREFIXES):
            msg = (
                f"test_command '{command}' not in allowlist. "
                "Use npm/yarn/python/pytest commands only."
            )
            logger.error("VerifierAgent: %s", msg)
            return False, msg

        cwd = Path(workspace_path) if workspace_path else Path.cwd()

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *command.split(),
                    cwd=str(cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                ),
                timeout=_TEST_TIMEOUT,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_TEST_TIMEOUT
            )
            output = stdout.decode("utf-8", errors="replace")[:4096]
            passed = (proc.returncode or 0) == 0
            return passed, output

        except asyncio.TimeoutError:
            msg = f"test_command timed out after {_TEST_TIMEOUT}s"
            logger.error("VerifierAgent: %s", msg)
            return False, msg
        except Exception as exc:
            msg = f"test_command execution error: {exc}"
            logger.error("VerifierAgent: %s", msg)
            return False, msg
