"""
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
"""

from __future__ import annotations

import asyncio
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
