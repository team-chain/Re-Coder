"""
Context Gate (6단계) — 민감정보 마스킹 + 품질 점수 계산.
Vision/LLM 전송 전 반드시 통과해야 하는 관문.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ── 터미널 제어 문자 / ANSI 코드 제거 ─────────────────────────────────
# script(1) 캡처 시 ANSI 색상 코드·ESC 시퀀스·\r 등이 섞여 텍스트가 깨짐
_ANSI_ESCAPE  = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
_CTRL_CHARS   = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')  # \t \n 제외


def strip_terminal_noise(text: str) -> str:
    """ANSI 이스케이프 코드와 제어 문자를 제거하고 CR+LF → LF로 정규화한다."""
    text = _ANSI_ESCAPE.sub('', text)
    text = _CTRL_CHARS.sub('', text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # 연속된 빈 줄 압축 (3줄 이상 → 2줄)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── 마스킹 패턴 ───────────────────────────────────────────────────────

_MASK_PATTERNS: list[tuple[re.Pattern, str]] = [
    # AWS 키
    (re.compile(r'AKIA[0-9A-Z]{16}'), '[MASKED_AWS_KEY]'),
    (re.compile(r'(?i)aws_secret_access_key\s*=\s*\S+'), 'aws_secret_access_key=[MASKED]'),
    (re.compile(r'(?i)aws_access_key_id\s*=\s*\S+'), 'aws_access_key_id=[MASKED]'),
    # API Key 패턴
    (re.compile(r'(?i)(api[_-]?key|apikey)\s*[=:]\s*\S+'), r'\1=[MASKED]'),
    # Bearer 토큰
    (re.compile(r'(?i)Bearer\s+[A-Za-z0-9\-._~+/]+=*'), 'Bearer [MASKED]'),
    # password / secret / token / private_key
    (re.compile(r'(?i)(password|passwd|secret|token|private_key|access_key)\s*[=:]\s*\S+'), r'\1=[MASKED]'),
    # DATABASE_URL / connection string
    (re.compile(r'(?i)(DATABASE_URL|REDIS_URL|MONGO_URI|DB_PASSWORD)\s*=\s*\S+'), r'\1=[MASKED]'),
    # JWT (3부분 base64)
    (re.compile(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'), '[MASKED_JWT]'),
    # Google / GitHub / Stripe / Slack 토큰 접두사 패턴
    (re.compile(r'\b(AIza[0-9A-Za-z\-_]{35})\b'), '[MASKED_GCP_KEY]'),
    (re.compile(r'\b(ghp_[A-Za-z0-9]{36}|github_pat_\w+)\b'), '[MASKED_GITHUB_TOKEN]'),
    (re.compile(r'\b(sk_live_[A-Za-z0-9]{24,}|sk_test_[A-Za-z0-9]{24,})\b'), '[MASKED_STRIPE_KEY]'),
    (re.compile(r'\bxox[baprs]-[A-Za-z0-9\-]+\b'), '[MASKED_SLACK_TOKEN]'),
    # 이메일
    (re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'), '[MASKED_EMAIL]'),
    # 전화번호 (한국)
    (re.compile(r'01[016789]-?\d{3,4}-?\d{4}'), '[MASKED_PHONE]'),
    # 신용카드 번호 (4-4-4-4 형식)
    (re.compile(r'\b(?:\d{4}[- ]){3}\d{4}\b'), '[MASKED_CARD]'),
]

# 파일 경로 익명화 (Windows / POSIX)
# Windows: C:\Users\username\... → username 이하만 유지
_WIN_PATH  = re.compile(r'[A-Za-z]:\\(?:[^\\/:*?"<>|\r\n]+\\)*([^\\/:*?"<>|\r\n]+)')
# POSIX: /home/user/... 또는 /Users/user/... → user 이하 경로만 유지
_UNIX_PATH = re.compile(r'/(?:home|Users|root)/[^/\s]+(/[^\s]*)')
# /var/folders/... 등 임시 경로 (파일명만 유지)
_TMPDIR_PATH = re.compile(r'/(?:tmp|var/folders|private/var)/\S+/([^/\s]+)')

# ── 에러 관련 키워드 (품질 점수 계산용) ──────────────────────────────
# 단어 단위 키워드: 많을수록 높은 품질 점수
_ERROR_KEYWORDS_HIGH = [
    # 공통 에러
    'traceback', 'exception', 'fatal', 'panic',
    'segmentation fault', 'core dumped',
    # Python
    'modulenotfounderror', 'importerror', 'syntaxerror', 'typeerror',
    'valueerror', 'nameerror', 'attributeerror', 'keyerror', 'indexerror',
    'runtimeerror', 'indentationerror', 'recursionerror',
    # JS / TS
    'referenceerror', 'unhandledpromiserejection', 'cannot find module',
    'module not found', 'compilation failed', 'error ts',
    # Java
    'nullpointerexception', 'classnotfoundexception', 'stackoverflow',
    'build failed',
    # Rust / Go
    'error[e', 'undefined:', 'goroutine',
    # DB
    'operationalerror', 'integrityerror', 'connection refused',
    # Docker / k8s
    'crashloopbackoff', 'imagepullbackoff', 'oomkilled',
    # 한국어
    '에러', '오류', '실패',
]

# 일반 에러 키워드 (낮은 가중치)
_ERROR_KEYWORDS_LOW = [
    'error', 'failed', 'failure', 'warning', 'warn',
    'no module', 'not found', 'permission denied',
    'command not found', 'no such file',
]


@dataclass
class GateResult:
    text:           str    # 마스킹 + 익명화된 텍스트
    quality_score:  float  # 0.0 ~ 1.0
    passed:         bool   # True면 전송 가능
    failure_reason: str    # 실패 이유 (passed=False 시)


def mask_secrets(text: str) -> str:
    """민감정보를 [MASKED] 처리한다."""
    for pattern, replacement in _MASK_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def anonymize_paths(text: str) -> str:
    """절대 경로에서 개인정보 구간(사용자명 등)을 제거하고 파일명만 남긴다."""
    text = _WIN_PATH.sub(r'\1', text)
    text = _UNIX_PATH.sub(r'\1', text)
    text = _TMPDIR_PATH.sub(r'\1', text)
    return text


def compute_quality_score(text: str) -> float:
    """에러 관련 키워드 밀도로 품질 점수를 계산한다 (0.0 ~ 1.0).

    - 고가중치 키워드 1개당 0.25점 (최대 1.0)
    - 저가중치 키워드로 최대 0.3점 보완
    - 텍스트가 짧으면 패널티 적용
    """
    if not text or len(text.strip()) < 10:
        return 0.0

    lower = text.lower()

    # 고가중치 키워드 점수
    high_hits = sum(1 for kw in _ERROR_KEYWORDS_HIGH if kw in lower)
    high_score = min(high_hits * 0.25, 1.0)

    # 저가중치 키워드 보완 점수 (고가중치가 낮을 때만 의미 있음)
    low_hits = sum(1 for kw in _ERROR_KEYWORDS_LOW if kw in lower)
    low_score = min(low_hits * 0.1, 0.3)

    score = min(high_score + low_score * (1.0 - high_score), 1.0)

    # 텍스트 길이 패널티: 20자 미만이면 절반
    if len(text.strip()) < 20:
        score *= 0.5

    return round(score, 2)


def run_gate(text: str, min_quality: float = 0.05) -> GateResult:
    """
    전체 Context Gate 파이프라인 실행.
    1. 민감정보 마스킹
    2. 경로 익명화
    3. 품질 점수 계산
    4. 통과 여부 결정

    min_quality를 0.05로 낮춰 단순 에러 메시지도 통과할 수 있게 함.
    (기존 0.1은 짧은 한 줄 에러가 걸러지는 문제가 있었음)
    """
    if not text or len(text.strip()) < 5:
        return GateResult(
            text="", quality_score=0.0, passed=False,
            failure_reason="텍스트가 너무 짧습니다."
        )

    # 1. 터미널 제어 문자 / ANSI 코드 제거 (script 캡처 파일 대응)
    clean  = strip_terminal_noise(text)
    masked = mask_secrets(clean)
    masked = anonymize_paths(masked)
    score  = compute_quality_score(masked)

    if score < min_quality:
        return GateResult(
            text=masked, quality_score=score, passed=False,
            failure_reason=f"품질 점수 부족 ({score:.2f} < {min_quality})"
        )

    return GateResult(text=masked, quality_score=score, passed=True, failure_reason="")
