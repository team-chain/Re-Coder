"""
Incident Fingerprint — 결정론적 사고 시그니처 (§35.1).

목표: 같은 root cause 의 사고는 같은 fingerprint 를 가져야 함.
      다른 root cause 는 다른 fingerprint.

알고리즘 (v0):
    fp = SHA256(
        error_type             |
        masked(error_message)  |
        last_file_basename     |
        normalized_stack_top_3
    )

마스킹 규칙 (Context Gate 16-pattern 의 부분집합):
    - workspace 절대 경로 → ``<WORKSPACE>``
    - 변수 값 (큰따옴표/작은따옴표 안 내용) → ``<VALUE>``
    - 16진수 hash (≥8자리) → ``<HASH>``
    - 숫자 (라인 번호 제외) → ``<NUM>``
    - timestamp → ``<TIMESTAMP>``
    - UUID → ``<UUID>``

normalize_stack_trace 는 stack trace 의 상위 3 frame 만 추출 (가변 path 제거).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePath


# ---------------------------------------------------------------------------
# Masking patterns — 결정론적이고 보수적인 마스킹 (false positive 무방, false negative 회피)
# ---------------------------------------------------------------------------


# 순서 중요 — 더 구체적인 패턴이 먼저.
# Secret 패턴은 HEX 패턴보다 먼저 — 더 구체적인 잡기 위함.
_MASK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # === Secret 패턴 (defensive — 디버그 로그에도 노출 차단) ===
    # AWS Access Key ID
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<SECRET>"),
    # AWS Secret Access Key (40 chars base64-like)
    (re.compile(r"\b[A-Za-z0-9+/]{40}\b(?![A-Za-z0-9+/])"), "<SECRET>"),
    # GitHub Personal Access Token
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "<SECRET>"),
    # GitHub fine-grained PAT
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b"), "<SECRET>"),
    # OpenAI API key
    (re.compile(r"\bsk-[A-Za-z0-9]{48}\b"), "<SECRET>"),
    # Stripe live key
    (re.compile(r"\bsk_live_[A-Za-z0-9]{24,}\b"), "<SECRET>"),
    # Slack token
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"), "<SECRET>"),
    # === 일반 패턴 ===
    # UUID (8-4-4-4-12)
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    # ISO-8601 timestamp
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"), "<TIMESTAMP>"),
    # Long hex hash (>=8 chars)
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<HASH>"),
    # Quoted values
    (re.compile(r'"[^"\n]*"'), '"<VALUE>"'),
    (re.compile(r"'[^'\n]*'"), "'<VALUE>'"),
    # Numbers (preserve "line N" idiom by separate handling)
    (re.compile(r"(?<!line\s)(?<!line:)(?<!:)\b\d+\b"), "<NUM>"),
]


_WORKSPACE_PATTERNS: list[re.Pattern[str]] = [
    # Windows-style absolute paths
    re.compile(r"[A-Z]:\\[\w\\\.\- ]+", re.IGNORECASE),
    # POSIX-style absolute paths
    re.compile(r"/(?:home|Users|sessions|opt|var|tmp|workspace)/[\w\-/\. ]+"),
]


def mask_for_fingerprint(text: str) -> str:
    """fingerprint 입력으로 안전한 형태로 마스킹.

    - 절대 경로 → ``<WORKSPACE>``
    - 숫자/hash/UUID/timestamp/문자열 리터럴 → 표준 placeholder
    - 결과는 보통 짧고 결정론적.
    """
    if not text:
        return ""
    out = text
    for pat in _WORKSPACE_PATTERNS:
        out = pat.sub("<WORKSPACE>", out)
    for pat, repl in _MASK_PATTERNS:
        out = pat.sub(repl, out)
    # 공백 정규화 — multiple whitespace → single space
    out = re.sub(r"\s+", " ", out).strip()
    return out


# ---------------------------------------------------------------------------
# Stack trace normalization
# ---------------------------------------------------------------------------


_STACK_FRAME_RE: re.Pattern[str] = re.compile(
    # Python: File "<path>", line N, in <func>
    r'File\s+"([^"]+)",\s+line\s+\d+,\s+in\s+(\S+)'
)


def normalize_stack_trace(stack_text: str, top_n: int = 3) -> list[str]:
    """stack trace 에서 상위 N 프레임의 (파일 basename, 함수명) 추출.

    가변 path 와 라인번호는 결정론성을 깨므로 제외 — basename + 함수명만.
    """
    if not stack_text:
        return []
    frames: list[str] = []
    for m in _STACK_FRAME_RE.finditer(stack_text):
        path, func = m.group(1), m.group(2)
        try:
            base = PurePath(path).name
        except Exception:
            base = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        frames.append(f"{base}::{func}")
        if len(frames) >= top_n:
            break
    return frames


# ---------------------------------------------------------------------------
# Public — build fingerprint
# ---------------------------------------------------------------------------


def build_incident_fingerprint(
    *,
    error_type: str,
    error_message: str,
    last_file: str | None = None,
    stack_trace: str | None = None,
    stack_top_n: int = 3,
) -> str:
    """결정론적 fingerprint (SHA256 hex, 64자) 반환.

    Args:
        error_type:    e.g. "ModuleNotFoundError", "ConnectionRefusedError"
        error_message: raw error message — 자동으로 마스킹 됨
        last_file:     마지막으로 만진 파일 (선택). basename 만 사용.
        stack_trace:   Python traceback 문자열 (선택)
        stack_top_n:   stack 의 상위 N 프레임 사용 (default 3)
    """
    parts: list[str] = []
    parts.append(f"type={error_type or ''}")
    parts.append(f"msg={mask_for_fingerprint(error_message or '')}")
    if last_file:
        try:
            base = PurePath(last_file).name
        except Exception:
            base = last_file
        parts.append(f"file={base}")
    if stack_trace:
        frames = normalize_stack_trace(stack_trace, top_n=stack_top_n)
        parts.append("stack=" + "|".join(frames))
    blob = "\n".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
