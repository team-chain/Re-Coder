"""
<<<<<<< HEAD
ReCoder Core — Context Gate (Section 18)

Responsible for:
  - Masking 16 categories of sensitive content before any text reaches an LLM
  - Computing quality scores to decide whether to invoke the LLM at all
  - Computing trigger scores to decide whether terminal output is worth analyzing
  - Generating error fingerprints for deduplication / cache hits
=======
Context Gate (설계서 v5 §6.7) - 민감정보 마스킹 + 품질 점수 계산.

핵심 원칙: raw text는 메모리에서만. 저장/전송/로그/S3에는 항상 masked text만.

마스킹 16종:
  1. AWS Access Key (AKIA...)         9. GitHub Token (ghp_ / github_pat_)
  2. AWS Secret Key                   10. Stripe Key (sk_live_ / sk_test_)
  3. API Key (api_key=, apikey=)      11. Slack Token (xox...)
  4. Bearer 토큰                      12. 이메일
  5. password / secret / token / ...  13. 한국 전화번호
  6. DATABASE_URL / REDIS_URL / ...   14. 신용카드
  7. JWT (eyJ...header.payload.sig)   15. ANSI 이스케이프 코드
  8. GCP Key (AIza...)                16. Windows 절대 경로

FastAPI 호출 시 이벤트 루프 블로킹을 방지하기 위해 async/await 사용.
>>>>>>> 74cf4369799da45d0fa49de67d56e58e01a2cc27
"""

from __future__ import annotations

import asyncio
<<<<<<< HEAD
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
=======
import re
from dataclasses import dataclass, field


_ANSI_ESCAPE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
_CTRL_CHARS  = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def strip_terminal_noise(text: str) -> str:
    text = _ANSI_ESCAPE.sub('', text)
    text = _CTRL_CHARS.sub('', text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


_MASK_PATTERNS = [
    ('AWS_ACCESS_KEY', re.compile(r'AKIA[0-9A-Z]{16}'),                      '[MASKED_AWS_KEY]'),
    ('AWS_SECRET',     re.compile(r'(?i)aws_secret_access_key\s*=\s*\S+'),   'aws_secret_access_key=[MASKED]'),
    ('AWS_ACCESS_KEY', re.compile(r'(?i)aws_access_key_id\s*=\s*\S+'),       'aws_access_key_id=[MASKED]'),
    ('API_KEY',        re.compile(r'(?i)(api[_-]?key|apikey)\s*[=:]\s*\S+'), r'\1=[MASKED]'),
    ('BEARER',         re.compile(r'(?i)Bearer\s+[A-Za-z0-9\-._~+/]+=*'),    'Bearer [MASKED]'),
    ('JWT',            re.compile(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'), '[MASKED_JWT]'),
    ('CREDENTIAL',     re.compile(r'(?i)(password|passwd|secret|token|private_key|access_key)\s*[=:]\s*\S+'), r'\1=[MASKED]'),
    ('DB_URL',         re.compile(r'(?i)(DATABASE_URL|REDIS_URL|MONGO_URI|DB_PASSWORD)\s*=\s*\S+'), r'\1=[MASKED]'),
    ('GCP_KEY',        re.compile(r'\b(AIza[0-9A-Za-z\-_]{35})\b'),                              '[MASKED_GCP_KEY]'),
    ('GITHUB_TOKEN',   re.compile(r'\b(ghp_[A-Za-z0-9]{36}|github_pat_\w+)\b'),                  '[MASKED_GITHUB_TOKEN]'),
    ('STRIPE_KEY',     re.compile(r'\b(sk_live_[A-Za-z0-9]{24,}|sk_test_[A-Za-z0-9]{24,})\b'),   '[MASKED_STRIPE_KEY]'),
    ('SLACK_TOKEN',    re.compile(r'\bxox[baprs]-[A-Za-z0-9\-]+\b'),                             '[MASKED_SLACK_TOKEN]'),
    ('EMAIL',          re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'), '[MASKED_EMAIL]'),
    ('PHONE_KR',       re.compile(r'01[016789]-?\d{3,4}-?\d{4}'), '[MASKED_PHONE]'),
    ('CARD',           re.compile(r'\b(?:\d{4}[- ]){3}\d{4}\b'), '[MASKED_CARD]'),
]


_WIN_PATH    = re.compile(r'[A-Za-z]:\\(?:[^\\/:*?"<>|\r\n]+\\)*([^\\/:*?"<>|\r\n]+)')
_UNIX_PATH   = re.compile(r'/(?:home|Users|root)/[^/\s]+(/[^\s]*)')
_TMPDIR_PATH = re.compile(r'/(?:tmp|var/folders|private/var)/\S+/([^/\s]+)')


_ERROR_KEYWORDS_HIGH = [
    'traceback', 'exception', 'fatal', 'panic', 'segmentation fault', 'core dumped',
    'modulenotfounderror', 'importerror', 'syntaxerror', 'typeerror', 'valueerror',
    'nameerror', 'attributeerror', 'keyerror', 'indexerror', 'runtimeerror',
    'indentationerror', 'recursionerror',
    'referenceerror', 'unhandledpromiserejection', 'cannot find module',
    'module not found', 'compilation failed', 'error ts',
    'nullpointerexception', 'classnotfoundexception', 'stackoverflow', 'build failed',
    'error[e', 'undefined:', 'goroutine',
    'operationalerror', 'integrityerror', 'connection refused',
    'crashloopbackoff', 'imagepullbackoff', 'oomkilled',
]

_ERROR_KEYWORDS_LOW = [
    'error', 'failed', 'failure', 'warning', 'warn',
    'no module', 'not found', 'permission denied',
    'command not found', 'no such file',
]


@dataclass
class GateResult:
    text:            str
    quality_score:   float
    passed:          bool
    failure_reason:  str
    masked_patterns: list = field(default_factory=list)
    needs_manual_description: bool = False   # 설계서 v5.7 §2.3 — 0.7 미만 수동 설명 권장 표식

    @property
    def masked_text(self) -> str:
        """설계서 v5의 필드명과 기존 코드의 text 필드를 함께 지원."""
        return self.text


def mask_secrets_with_stats(text: str):
    matched = []
    seen = set()
    for name, pattern, replacement in _MASK_PATTERNS:
        new_text, count = pattern.subn(replacement, text)
        if count > 0 and name not in seen:
            seen.add(name)
            matched.append(name)
        text = new_text
    return text, matched


def mask_secrets(text: str) -> str:
    masked, _ = mask_secrets_with_stats(text)
    return masked


def anonymize_paths(text: str) -> str:
    text = _WIN_PATH.sub(r'\1', text)
    text = _UNIX_PATH.sub(r'\1', text)
    text = _TMPDIR_PATH.sub(r'\1', text)
    return text


def compute_quality_score(text: str) -> float:
    if not text or len(text.strip()) < 10:
        return 0.0
    lower = text.lower()
    high_hits = sum(1 for kw in _ERROR_KEYWORDS_HIGH if kw in lower)
    high_score = min(high_hits * 0.25, 1.0)
    low_hits = sum(1 for kw in _ERROR_KEYWORDS_LOW if kw in lower)
    low_score = min(low_hits * 0.1, 0.3)
    score = min(high_score + low_score * (1.0 - high_score), 1.0)
    if re.search(r'\bline\s+\d+\b|:\d+:\d+', text, re.IGNORECASE):
        score = min(score + 0.1, 1.0)
    words = re.findall(r'\S{3,}', text.lower())
    if len(words) >= 12:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio < 0.35:
            score *= 0.6
    if len(text.strip()) < 20:
        score *= 0.5
    return round(score, 2)


def _run_gate_sync(text: str, min_quality: float = 0.7) -> GateResult:
    """
    설계서 v5.7 §2.3 기준.
    LLM 호출 조건: trigger_score >= 70 AND quality_score >= 0.7
    min_quality 기본값 0.7.

    동기 구현 버전. asyncio.get_event_loop().run_in_executor() 에서 호출됨.
    """
    if not text or len(text.strip()) < 5:
        return GateResult(
            text="", quality_score=0.0, passed=False,
            failure_reason="텍스트가 너무 짧습니다.",
            masked_patterns=[],
        )
    clean = strip_terminal_noise(text)
    masked, patterns = mask_secrets_with_stats(clean)
    before_path = masked
    masked = anonymize_paths(masked)
    if masked != before_path:
        patterns.append('PATH')
    if clean != text:
        patterns.append('ANSI')
    score = compute_quality_score(masked)
    if score < min_quality:
        return GateResult(
            text=masked, quality_score=score, passed=False,
            failure_reason=f"품질 점수 부족 ({score:.2f} < {min_quality})",
            masked_patterns=patterns,
            needs_manual_description=True,
        )
    return GateResult(
        text=masked, quality_score=score, passed=True,
        failure_reason="",
        masked_patterns=patterns,
        needs_manual_description=False,
    )


async def run_gate(text: str, min_quality: float = 0.7) -> GateResult:
    """
    비동기 Context Gate 호출 (FastAPI 호환).

    내부 동기 처리를 asyncio.get_event_loop().run_in_executor() 로
    감싸서 이벤트 루프를 블로킹하지 않음.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run_gate_sync, text, min_quality)
>>>>>>> 74cf4369799da45d0fa49de67d56e58e01a2cc27
