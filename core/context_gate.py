"""
ReCoder Core — Context Gate (Section 18)

Responsible for:
  - Masking 16 categories of sensitive content before any text reaches an LLM
  - Computing quality scores to decide whether to invoke the LLM at all
  - Computing trigger scores to decide whether terminal output is worth analyzing
  - Generating error fingerprints for deduplication / cache hits
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Optional

from schemas import AnalyzeRequest, MaskingResult, QualityScore

# ---------------------------------------------------------------------------
# Masking pattern registry
# 16 pattern categories compiled once at import time.
# ---------------------------------------------------------------------------

MASKING_PATTERNS: list[tuple[str, str]] = [
    ("AWS_ACCESS_KEY",  r"AKIA[0-9A-Z]{16}"),
    ("AWS_SECRET_KEY",  r"(?i)aws[_\-\s]?secret[_\-\s]?(?:access[_\-\s]?)?key[^=\n]*=[^\n]*"),
    ("API_KEY",         r"(?i)api[_\-\s]?key[^=\n]*=[^\s\n]+"),
    ("BEARER_TOKEN",    r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    ("PASSWORD_VAR",    r"(?i)(?:password|secret|token|private_key)\s*=\s*[^\s\n]+"),
    ("DATABASE_URL",    r"(?i)(?:database_url|redis_url|mongo_uri)\s*=\s*[^\s\n]+"),
    ("JWT",             r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    ("GCP_KEY",         r'"type"\s*:\s*"service_account"'),
    ("GITHUB_TOKEN",    r"gh[pousr]_[A-Za-z0-9_]{36,}"),
    ("STRIPE_KEY",      r"sk_(?:live|test)_[A-Za-z0-9]{24,}"),
    ("SLACK_TOKEN",     r"xox[baprs]-[A-Za-z0-9\-]+"),
    ("EMAIL",           r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
    ("KR_PHONE",        r"(?:010|011|016|017|018|019)-?\d{3,4}-?\d{4}"),
    ("CREDIT_CARD",     r"\b(?:\d[ \-]?){13,16}\b"),
    ("ANSI_ESCAPE",     r"\x1b\[[0-9;]*[mGKHF]"),
    # WIN_ABS_PATH: keep only the filename (last component), mask directory prefix
    ("WIN_ABS_PATH",    r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)+([^\\/:*?\"<>|\r\n]*)"),
]


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def _replace_with_count(
    text: str,
    pattern: re.Pattern,
    repl,
) -> tuple[str, int]:
    """Apply *repl* to all matches in *text*, returning (new_text, match_count)."""
    count = 0

    def _counter(m: re.Match) -> str:
        nonlocal count
        count += 1
        return repl(m)

    new_text = pattern.sub(_counter, text)
    return new_text, count


# ---------------------------------------------------------------------------
# ContextGate
# ---------------------------------------------------------------------------


class ContextGate:
    """
    Stateless masker + stateful fingerprint cache.

    One instance is expected to live for the lifetime of the Core process
    (singleton pattern recommended by the caller).
    """

    _MASK_VERSION = "1.0"
    _FINGERPRINT_TTL = 60.0  # seconds

    def __init__(self) -> None:
        # Pre-compile all patterns once at startup.
        self._compiled: list[tuple[str, re.Pattern]] = [
            (name, re.compile(pattern)) for name, pattern in MASKING_PATTERNS
        ]
        # fingerprint -> expiry timestamp
        self._fingerprint_cache: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def mask(self, content: str) -> MaskingResult:
        """
        Apply all 16 masking patterns to *content* asynchronously.

        WIN_ABS_PATH substitutions retain the filename (last path component)
        while replacing the directory prefix with ``[WIN_ABS_PATH]``.
        All other matches are replaced with ``[<PATTERN_NAME>]``.

        Runs the CPU-bound work in the default thread-pool executor so the
        event loop is never blocked for large inputs.
        """
        loop = asyncio.get_running_loop()
        masked_content, mask_count = await loop.run_in_executor(
            None, self._mask_sync, content
        )
        return MaskingResult(
            masked_content=masked_content,
            mask_count=mask_count,
            mask_version=self._MASK_VERSION,
        )

    # ------------------------------------------------------------------
    # Scoring helpers (synchronous — called from FSM transitions)
    # ------------------------------------------------------------------

    def compute_quality_score(
        self,
        masked_content: str,
        original_request: AnalyzeRequest,
    ) -> QualityScore:
        """
        Return a QualityScore in [0.0, 1.0] for *masked_content*.

        Weights
        -------
        has_traceback         -> 0.30
        has_project_path      -> 0.20
        error_message_length  -> 0.20  (saturates at 200 chars -> full credit)
        masked_info_density   -> 0.15  (fraction of non-whitespace chars remaining)
        has_related_files     -> 0.15
        """
        content_lower = masked_content.lower()

        # traceback / stack-trace presence
        has_traceback = bool(
            re.search(r"traceback|stack trace|at line \d+|\.py.*line \d+", content_lower)
        )

        # project-internal file path reference
        workspace = original_request.workspace_path.replace("\\", "/").lower()
        has_project_path = workspace in masked_content.replace("\\", "/").lower()

        # error message length heuristic
        error_line_match = re.search(
            r"(?:error|exception|fatal)[^\n]*", masked_content, re.IGNORECASE
        )
        error_message_length = len(error_line_match.group(0)) if error_line_match else 0

        # masked info density: fraction of non-whitespace content remaining
        non_ws = len(re.sub(r"\s+", "", masked_content))
        masked_info_density = min(1.0, non_ws / max(len(re.sub(r"\s+", "", masked_content)), 1))

        # related files collected
        has_related_files = bool(original_request.project_files_summary)

        # weighted sum
        score = (
            (0.30 if has_traceback else 0.0)
            + (0.20 if has_project_path else 0.0)
            + (0.20 * min(error_message_length / 200.0, 1.0))
            + (0.15 * masked_info_density)
            + (0.15 if has_related_files else 0.0)
        )
        score = max(0.0, min(1.0, score))

        return QualityScore(
            score=score,
            has_traceback=has_traceback,
            has_project_path=has_project_path,
            error_message_length=error_message_length,
            masked_info_density=masked_info_density,
            has_related_files=has_related_files,
        )

    def compute_trigger_score(self, terminal_output: str) -> float:
        """
        Rule-based trigger score in [0, 100].

        The higher the score, the more likely the terminal output represents
        a genuine error worth sending to the LLM.

        Rules (additive)
        ----------------
        +40  Traceback / stack trace present
        +25  "Error" or "Exception" keyword present
        +15  Exit code non-zero hint (e.g. "exit 1", "returned non-zero")
        +10  Multiple error lines (>= 3 lines containing "error")
        +10  "Fatal" or "Critical" present
        -20  Output is very short (< 30 chars after stripping) -- likely a prompt
        """
        if not terminal_output:
            return 0.0

        score = 0.0
        lower = terminal_output.lower()

        if re.search(r"traceback|stack trace", lower):
            score += 40.0

        if re.search(r"\berror\b|\bexception\b", lower):
            score += 25.0

        if re.search(r"exit\s*(?:code\s*)?\d+|returned non.zero|exited with", lower):
            score += 15.0

        error_lines = [ln for ln in terminal_output.splitlines() if "error" in ln.lower()]
        if len(error_lines) >= 3:
            score += 10.0

        if re.search(r"\bfatal\b|\bcritical\b", lower):
            score += 10.0

        if len(terminal_output.strip()) < 30:
            score -= 20.0

        return max(0.0, min(100.0, score))

    def compute_error_fingerprint(
        self,
        masked_content: str,
        request: AnalyzeRequest,
    ) -> str:
        """
        Stable dedup key = SHA-256( error_type + last_project_file + error_msg ).

        error_type         : first Error/Exception class name found, else "Unknown"
        last_project_file  : last occurrence of a project-relative source file path
        error_msg          : first non-empty error-description line (truncated to 120)
        """
        # 1. error type
        error_type = "Unknown"
        type_match = re.search(
            r"([A-Za-z][A-Za-z0-9_]*(?:Error|Exception|Fault|Panic))", masked_content
        )
        if type_match:
            error_type = type_match.group(1)

        # 2. last project-internal file reference
        workspace_norm = request.workspace_path.replace("\\", "/")
        file_matches = re.findall(
            r"(?:" + re.escape(workspace_norm) + r")?[./]?[\w/\-]+\.(?:py|ts|js|go|java|rb)",
            masked_content,
        )
        last_project_file = file_matches[-1] if file_matches else ""

        # 3. error message text
        error_msg = ""
        err_match = re.search(
            r"(?:Error|Exception|Traceback)[^\n]*\n?(.*)", masked_content, re.IGNORECASE
        )
        if err_match:
            error_msg = err_match.group(1).strip()[:120]

        raw = f"{error_type}|{last_project_file}|{error_msg}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def check_fingerprint_cache(self, fingerprint: str) -> bool:
        """Return True if *fingerprint* was seen within the last 60 seconds."""
        expiry = self._fingerprint_cache.get(fingerprint)
        if expiry is None:
            return False
        if time.monotonic() < expiry:
            return True
        # Expired -- remove stale entry
        del self._fingerprint_cache[fingerprint]
        return False

    def update_fingerprint_cache(self, fingerprint: str) -> None:
        """Record *fingerprint* with a 60-second TTL."""
        self._fingerprint_cache[fingerprint] = time.monotonic() + self._FINGERPRINT_TTL

    # ------------------------------------------------------------------
    # Private sync worker
    # ------------------------------------------------------------------

    def _mask_sync(self, content: str) -> tuple[str, int]:
        """CPU-bound masking -- intended to run in a thread-pool executor."""
        result = content
        total_masks = 0

        for name, pattern in self._compiled:
            if name == "WIN_ABS_PATH":
                # Keep filename (last capture group), mask directory prefix.
                def _win_replacer(m: re.Match) -> str:
                    filename = m.group(1) if m.lastindex and m.group(1) else ""
                    return f"[WIN_ABS_PATH]/{filename}" if filename else "[WIN_ABS_PATH]"

                new_result, count = _replace_with_count(result, pattern, _win_replacer)
            else:
                replacement = f"[{name}]"
                new_result, count = _replace_with_count(
                    result, pattern, lambda m, r=replacement: r
                )

            result = new_result
            total_masks += count

        return result, total_masks
