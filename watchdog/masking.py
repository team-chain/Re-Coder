"""
watchdog/masking.py — 16종 민감정보 마스킹 유틸 (설계 §5.4.1).

ReCoder core/context_gate.py 의 마스킹 로직을 watchdog 데몬에서 재사용하기 위해
표준 라이브러리만으로 복제한 단순 구현이다. core 모듈에 직접 의존하지 않으므로
EC2 에서 core 코드 배포 없이 단독으로 동작한다.

사용:
    from watchdog.masking import mask_text
    cleaned = mask_text(raw_log_excerpt)

원칙:
  - raw text 는 메모리에서만 유지.
  - 저장(incident.jsonl) / 전송(Discord) 직전에 반드시 mask_text 통과.
  - 16종 패턴은 가능한 한 conservative 하게 — 잘못된 마스킹 < 노출.
"""

from __future__ import annotations

import re
from typing import Iterable, Pattern, Tuple

MASK_VERSION = "watchdog-mask-v1"

# (label, compiled_regex, replacement) — 순서가 중요하다.
# JWT / 토큰류는 광범위 매칭이 가능하므로 먼저 처리한다.
_MASK_RULES: Tuple[Tuple[str, Pattern[str], str], ...] = (
    # 1. AWS Access Key (AKIA...)
    ("AWS_ACCESS_KEY", re.compile(r"AKIA[0-9A-Z]{16}"), "[MASKED_AWS_KEY]"),
    # 2. AWS Secret Key (env-style assignment)
    ("AWS_SECRET_KEY",
     re.compile(r"(?i)aws[_\-\s]?secret[_\-\s]?(?:access[_\-\s]?)?key\s*[=:]\s*\S+"),
     "aws_secret_access_key=[MASKED]"),
    # 3. API Key
    ("API_KEY",
     re.compile(r"(?i)(api[_\-]?key|apikey)\s*[=:]\s*\S+"),
     r"\1=[MASKED]"),
    # 4. Bearer 토큰
    ("BEARER_TOKEN",
     re.compile(r"(?i)Bearer\s+[A-Za-z0-9\-._~+/]+=*"),
     "Bearer [MASKED]"),
    # 5. password / secret / token / private_key 일반 변수
    ("PASSWORD_VAR",
     re.compile(r"(?i)(password|passwd|secret|token|private_key|access_key)\s*[=:]\s*\S+"),
     r"\1=[MASKED]"),
    # 6. DATABASE_URL / REDIS_URL / MONGO_URI
    ("DATABASE_URL",
     re.compile(r"(?i)(database_url|redis_url|mongo_uri|db_password)\s*[=:]\s*\S+"),
     r"\1=[MASKED]"),
    # 7. JWT (header.payload.signature)
    ("JWT",
     re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
     "[MASKED_JWT]"),
    # 8. GCP Key (AIza... 39chars) + service_account marker
    ("GCP_KEY",
     re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
     "[MASKED_GCP_KEY]"),
    ("GCP_SA",
     re.compile(r'"type"\s*:\s*"service_account"'),
     '"type": "[MASKED_SA]"'),
    # 9. GitHub Token (ghp_ / github_pat_ / gho_ / ghu_ / ghs_ / ghr_)
    ("GITHUB_TOKEN",
     re.compile(r"\b(ghp_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}|gh[ousr]_[A-Za-z0-9]{36,})\b"),
     "[MASKED_GITHUB_TOKEN]"),
    # 10. Stripe Key (sk_live_ / sk_test_)
    ("STRIPE_KEY",
     re.compile(r"\b(sk_live_[A-Za-z0-9]{20,}|sk_test_[A-Za-z0-9]{20,})\b"),
     "[MASKED_STRIPE_KEY]"),
    # 11. Slack Token (xox...)
    ("SLACK_TOKEN",
     re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
     "[MASKED_SLACK_TOKEN]"),
    # 12. 이메일
    ("EMAIL",
     re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
     "[MASKED_EMAIL]"),
    # 13. 한국 전화번호
    ("KR_PHONE",
     re.compile(r"(?:010|011|016|017|018|019)-?\d{3,4}-?\d{4}"),
     "[MASKED_PHONE]"),
    # 14. 신용카드 (간단한 13~16자리 숫자)
    ("CREDIT_CARD",
     re.compile(r"\b(?:\d[ \-]?){13,16}\b"),
     "[MASKED_CC]"),
    # 15. ANSI 이스케이프
    ("ANSI_ESCAPE",
     re.compile(r"\x1b\[[0-9;]*[mGKHF]"),
     ""),
    # 16. Windows 절대경로 (디렉토리 경로 마스킹, 파일명만 유지)
    ("WIN_ABS_PATH",
     re.compile(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)+([^\\/:*?\"<>|\r\n]*)"),
     r"[MASKED_PATH]\\\1"),
)

# 컨트롤 문자 / CR 정규화
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_terminal_noise(text: str) -> str:
    """ANSI escape + control char 제거 + CRLF -> LF."""
    if not text:
        return ""
    # ANSI 는 _MASK_RULES 의 ANSI_ESCAPE 가 처리하지만, 컨트롤 문자는 별도.
    text = _CTRL_CHARS.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def mask_text(text: str) -> str:
    """입력 문자열에 16종 패턴을 적용해 마스킹된 문자열을 돌려준다.

    None / 빈 문자열은 빈 문자열로 반환. 예외를 던지지 않는다.
    """
    if not text:
        return ""
    out = strip_terminal_noise(str(text))
    for _label, pattern, replacement in _MASK_RULES:
        try:
            out = pattern.sub(replacement, out)
        except re.error:  # pragma: no cover — 정상 패턴이므로 도달하지 않음
            continue
    return out


def mask_lines(lines: Iterable[str], max_lines: int = 50, max_line_len: int = 500) -> list[str]:
    """다중 라인 로그를 마스킹 + 길이 제한.

    Discord embed / incident.jsonl 에 들어갈 logs_excerpt 용도.
    """
    result: list[str] = []
    for i, line in enumerate(lines):
        if i >= max_lines:
            result.append(f"... ({i - max_lines + 1}+ more lines truncated)")
            break
        masked = mask_text(line)
        if len(masked) > max_line_len:
            masked = masked[:max_line_len] + "...[TRUNCATED]"
        result.append(masked)
    return result


__all__ = ["mask_text", "mask_lines", "strip_terminal_noise", "MASK_VERSION"]
