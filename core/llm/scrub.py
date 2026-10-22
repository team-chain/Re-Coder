"""외부 공급자로 보내기 전 비밀값 가리기(2.0.36).

품질 등급의 기존 공급자(사용자가 처음 고른 곳)가 아닌 다른 공급자 — 절약 공급자, OpenAI 호환
API(DeepSeek 등) — 로 나가는 문맥은 여기를 거친다. 코드가 깨지지 않게 **확실한 비밀 값**만 가린다
(context_gate.scrub_build_source 와 같은 규칙 + .env 줄·Bearer·JWT).

`password = req.body.password` 같은 코드는 그대로 둔다. 가린 개수는 원장에 남는다(값은 남기지 않는다).
"""
from __future__ import annotations

import re

try:
    from context_gate import scrub_build_source
except ImportError:  # pragma: no cover
    from core.context_gate import scrub_build_source  # type: ignore

_EXTRA: list[tuple[re.Pattern, str]] = [
    #: .env 형식 줄 — 대문자 이름 + 문자 그대로의 값. `${X}`·process.env 참조는 남긴다.
    (re.compile(r"(?m)^([ \t]{0,32}(?:export[ \t]+)?[A-Z0-9_]{0,64}(?:SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY|CLIENT_SECRET)[A-Z0-9_]{0,64}[ \t]{0,8}=[ \t]{0,8})"
                r"(?![\"']?\$\{)(?![\"']?\[MASKED)(?!process\.env)(?!os\.environ)(?:\"[^\"\n]+\"|'[^'\n]+'|[^\s#\n]+)"), r"\1[MASKED]"),
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9\-._~+/]{16,}=*"), r"\1[MASKED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[MASKED_JWT]"),
    (re.compile(r"\bsk_test_[A-Za-z0-9]{20,}\b"), "[MASKED_STRIPE_KEY]"),
]


def scrub_for_external(text: str, code: bool = False) -> tuple[str, int]:
    """(가린 글, 가린 곳 수).

    code=True — 결과가 코드 파일이 되는 작업. 코드 생성 경로가 이미 쓰는 규칙(scrub_build_source)만
    적용한다. 그 경로가 넣은 번호 자리표시([MASKED_AWS_KEY_1])를 덮으면 결과 파일에 원래 값을 되돌릴
    수 없고, 대문자 상수(`SECRET_KEY = "dev"`)를 가리면 AI 가 다시 쓴 파일이 깨진다.
    """
    if not text:
        return text, 0
    count = 0
    out = scrub_build_source(text)
    if out != text:
        count += max(1, out.count("[MASKED") - text.count("[MASKED"))
    if code:
        return out, count
    for pattern, replacement in _EXTRA:
        out, n = pattern.subn(replacement, out)
        count += n
    return out, count
