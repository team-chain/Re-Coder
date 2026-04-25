"""
Context Gate (6단계) — 민감정보 마스킹 + 품질 점수 계산.
Vision/LLM 전송 전 반드시 통과해야 하는 관문.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ── 마스킹 패턴 ───────────────────────────────────────────────────────

_MASK_PATTERNS: list[tuple[re.Pattern, str]] = [
    # AWS 키
    (re.compile(r'AKIA[0-9A-Z]{16}'), '[MASKED_AWS_KEY]'),
    (re.compile(r'(?i)aws_secret_access_key\s*=\s*\S+'), 'aws_secret_access_key=[MASKED]'),
    # API Key 패턴
    (re.compile(r'(?i)(api[_-]?key|apikey)\s*[=:]\s*\S+'), r'\1=[MASKED]'),
    # Bearer 토큰
    (re.compile(r'(?i)Bearer\s+[A-Za-z0-9\-._~+/]+=*'), 'Bearer [MASKED]'),
    # password / secret / token
    (re.compile(r'(?i)(password|secret|token)\s*=\s*\S+'), r'\1=[MASKED]'),
    # DATABASE_URL
    (re.compile(r'(?i)DATABASE_URL\s*=\s*\S+'), 'DATABASE_URL=[MASKED]'),
    # JWT (3부분 base64)
    (re.compile(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'), '[MASKED_JWT]'),
    # 이메일
    (re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'), '[MASKED_EMAIL]'),
    # 전화번호 (한국)
    (re.compile(r'01[016789]-?\d{3,4}-?\d{4}'), '[MASKED_PHONE]'),
]

# 파일 경로 익명화 (Windows / POSIX)
_WIN_PATH  = re.compile(r'[A-Za-z]:\\(?:[^\\/:*?"<>|\r\n]+\\)*([^\\/:*?"<>|\r\n]+)')
_UNIX_PATH = re.compile(r'/(?:home|Users)/[^/\s]+(/[^\s]*)')

# 에러 관련 키워드 (품질 점수 계산용)
_ERROR_KEYWORDS = [
    'error', 'exception', 'traceback', 'failed', 'fatal',
    'no module', 'importerror', 'modulenotfounderror',
    'syntaxerror', 'typeerror', 'valueerror', 'nameerror',
    'attributeerror', 'keyerror', 'indexerror',
    '에러', '오류', '실패',
]


@dataclass
class GateResult:
    text:          str    # 마스킹 + 익명화된 텍스트
    quality_score: float  # 0.0 ~ 1.0
    passed:        bool   # True면 전송 가능
    failure_reason: str   # 실패 이유 (passed=False 시)


def mask_secrets(text: str) -> str:
    """민감정보를 [MASKED] 처리한다."""
    for pattern, replacement in _MASK_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def anonymize_paths(text: str) -> str:
    """절대 경로에서 파일명만 남긴다."""
    text = _WIN_PATH.sub(r'\1', text)
    text = _UNIX_PATH.sub(r'\1', text)
    return text


def compute_quality_score(text: str) -> float:
    """에러 관련 텍스트 비율로 품질 점수 계산 (0.0 ~ 1.0)."""
    if not text or len(text.strip()) < 10:
        return 0.0
    lower = text.lower()
    matched = sum(1 for kw in _ERROR_KEYWORDS if kw in lower)
    # 키워드 3개 이상이면 만점
    score = min(matched / 3.0, 1.0)
    # 텍스트 길이 보너스 (20자 미만이면 패널티)
    if len(text.strip()) < 20:
        score *= 0.5
    return round(score, 2)


def run_gate(text: str, min_quality: float = 0.1) -> GateResult:
    """
    전체 Context Gate 파이프라인 실행.
    1. 민감정보 마스킹
    2. 경로 익명화
    3. 품질 점수 계산
    4. 통과 여부 결정
    """
    if not text or len(text.strip()) < 5:
        return GateResult(
            text="", quality_score=0.0, passed=False,
            failure_reason="텍스트가 너무 짧습니다."
        )

    masked = mask_secrets(text)
    masked = anonymize_paths(masked)
    score  = compute_quality_score(masked)

    if score < min_quality:
        return GateResult(
            text=masked, quality_score=score, passed=False,
            failure_reason=f"품질 점수 부족 ({score:.2f} < {min_quality})"
        )

    return GateResult(text=masked, quality_score=score, passed=True, failure_reason="")
