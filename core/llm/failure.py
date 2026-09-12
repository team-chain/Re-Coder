"""LLM 호출 실패 → 사용자에게 그대로 보여줄 한 문장.

배경
    데모에서 채팅·코드 생성이 죽었을 때 사용자가 본 것은 "응답을 가져오지
    못했어요" 또는 provider 원문(429 JSON 덩어리)이었다. 원인도, 다음에 뭘
    하면 되는지도 없었다. 여기서는 **원인 분류 + 다음 행동**을 담은 문장을
    만든다. provider 의 내부 오류 문자열은 노출하지 않는다.

원칙
    1. `LLMError` 라면 이미 분류된 `error_type` 을 **먼저** 믿는다 —
       메시지 문자열 검색은 provider 문구가 바뀌면 조용히 깨진다.
    2. 분류가 없으면(일반 예외) 메시지 토큰으로 추정한다.
    3. 둘 다 실패하면 예외 클래스 이름만 남긴다 — 원문 전체를 붙이면
       버킷 이름·경로 같은 내부 정보가 사용자 화면으로 샌다.

이 모듈은 routes/deploy.py 에 있던 `_public_ai_failure_reason` 을 승격한
것이다(채팅 라우트도 같은 문장이 필요해졌다). 문장은 기존 그대로 유지한다
— test_infra_file_generation 이 "요청 한도" 문구를 검사한다.
"""
from __future__ import annotations

from .base import LLMError, LLMErrorType

#: error_type → 사용자 문장. UNKNOWN 은 의도적으로 뺐다(메시지 추정으로 폴백).
_REASON_BY_TYPE: dict[LLMErrorType, str] = {
    LLMErrorType.THROTTLING:       "AI 제공자의 요청 한도에 도달했습니다.",
    LLMErrorType.QUOTA_EXCEEDED:   "AI 제공자의 요청 한도에 도달했습니다.",
    LLMErrorType.ACCESS_DENIED:    "AI 인증 정보 또는 자격증명을 확인하지 못했습니다.",
    LLMErrorType.MODEL_NOT_FOUND:  "설정된 AI 모델을 사용할 수 없습니다.",
    LLMErrorType.CONTEXT_TOO_LONG: "요청이 너무 길어 AI 가 처리하지 못했습니다.",
    LLMErrorType.SERVICE_ERROR:    "AI 제공자 쪽 일시적인 오류입니다.",
}


def public_ai_failure_reason(exc: Exception | None) -> str:
    """Return an actionable reason without exposing provider error details."""
    if exc is None:
        return "AI 에이전트를 초기화하지 못했습니다."

    if isinstance(exc, LLMError):
        reason = _REASON_BY_TYPE.get(exc.error_type)
        if reason:
            return reason
        #: UNKNOWN 으로 분류된 LLMError 는 원인 예외(raw)의 메시지가
        #: 더 구체적일 수 있다 — 예: Gemini 폴백 체인이 429 를 감싼 경우.
        if exc.raw is not None:
            return public_ai_failure_reason(exc.raw)

    message = str(exc).lower()
    if any(token in message for token in (
        "rate limit", "throttl", "quota", "429", "resource_exhausted", "resource exhausted",
    )):
        return "AI 제공자의 요청 한도에 도달했습니다."
    if any(token in message for token in (
        "credential", "api key", "api_key", "unauthorized", "forbidden", "auth",
    )):
        return "AI 인증 정보 또는 자격증명을 확인하지 못했습니다."
    if any(token in message for token in ("timeout", "timed out")):
        return "AI 제공자의 응답 시간이 초과됐습니다."
    if any(token in message for token in ("connection", "network", "dns")):
        return "AI 제공자와 네트워크 연결에 실패했습니다."
    return f"AI 제공자 호출에 실패했습니다 ({exc.__class__.__name__})."
