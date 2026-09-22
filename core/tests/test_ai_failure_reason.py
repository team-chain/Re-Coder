"""LLM 실패 원인 노출 — 채팅 패널 "원인 미표시" 결함(보드 카드)의 코어 쪽 수정.

여기서 막는 사고는 두 가지다.
1) 사용자가 원인을 못 본다 — 예전 /api/chat 은 provider 예외 원문을 detail 에
   실었고(429 JSON 덩어리), 웹뷰는 그마저 버리고 고정 문구만 띄웠다. 코어는
   이제 **분류된 문장**을 내려보낸다.
2) 내부 정보가 샌다 — provider 원문에는 모델 ID·내부 경로가 섞여 나올 수
   있다. 분류 문장에는 원문을 포함하지 않는다.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from llm.base import LLMError, LLMErrorType  # noqa: E402
from llm.failure import public_ai_failure_reason  # noqa: E402


# ---------------------------------------------------------------------------
# 분류기 단위
# ---------------------------------------------------------------------------


def test_LLMError_분류를_문자열_추정보다_먼저_믿는다() -> None:
    """메시지에 아무 단서가 없어도 error_type 만으로 분류돼야 한다."""
    exc = LLMError("opaque provider blob", LLMErrorType.THROTTLING)
    assert "요청 한도" in public_ai_failure_reason(exc)


def test_UNKNOWN_LLMError는_감싼_원인으로_한번_더_판정한다() -> None:
    """Gemini 폴백 체인이 429 를 UNKNOWN 으로 감싸는 실제 경로."""
    raw = RuntimeError("429 RESOURCE_EXHAUSTED: rate limit for model x")
    exc = LLMError("Gemini 전체 폴백 체인 실패: ...", LLMErrorType.UNKNOWN, raw=raw)
    assert "요청 한도" in public_ai_failure_reason(exc)


@pytest.mark.parametrize("message, expected", [
    ("rate limit exceeded", "요청 한도"),
    ("invalid api key provided", "자격증명"),
    ("read timed out after 30s", "응답 시간"),
    ("connection refused", "네트워크"),
])
def test_일반_예외는_메시지_토큰으로_추정한다(message: str, expected: str) -> None:
    assert expected in public_ai_failure_reason(RuntimeError(message))


def test_분류_불가_예외는_원문을_노출하지_않는다() -> None:
    """원문에 섞인 내부 정보(경로·버킷 이름 등)가 사용자 화면으로 새면 안 된다."""
    reason = public_ai_failure_reason(ValueError("s3://internal-bucket/prompt.txt broke"))
    assert "internal-bucket" not in reason
    assert "ValueError" in reason, "무엇이 실패했는지 최소 단서(클래스명)는 남긴다"


# ---------------------------------------------------------------------------
# /api/chat 배선 — 웹뷰 채팅 패널이 detail 을 그대로 띄운다
# ---------------------------------------------------------------------------


def _run_chat(monkeypatch: pytest.MonkeyPatch, error: Exception):
    from api.routes import analyze
    import llm.router as router_mod

    class _Router:
        def call(self, *args, **kwargs):
            raise error

    monkeypatch.setattr(router_mod, "get_router", lambda: _Router())

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(analyze.chat_route(analyze.ChatRequest(message="배포 어떻게 해?")))
    return exc.value


def test_채팅_실패는_분류된_원인을_detail에_싣는다(monkeypatch) -> None:
    err = LLMError(
        '429 {"error": {"status": "RESOURCE_EXHAUSTED", "internal": "projects/x/models/y"}}',
        LLMErrorType.QUOTA_EXCEEDED,
    )
    result = _run_chat(monkeypatch, err)
    assert result.status_code == 500
    detail = str(result.detail)
    assert "AI 대화 실패" in detail
    assert "요청 한도" in detail, "원인 분류가 detail 에 없다 — 화면에 원인이 안 뜬다"
    #: [음성 대조] provider 원문이 그대로 새면 안 된다.
    assert "RESOURCE_EXHAUSTED" not in detail
    assert "projects/x" not in detail


def test_빈_응답은_전용_문장을_유지한다(monkeypatch) -> None:
    """분류기를 거치면 '(RuntimeError)' 로 뭉개진다 — 이 경우는 원인이 명확하다."""
    from api.routes import analyze
    import llm.router as router_mod

    class _Router:
        def call(self, *args, **kwargs):
            class _R:
                text = "   "
                model_used = "m"
            return _R()

    monkeypatch.setattr(router_mod, "get_router", lambda: _Router())

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(analyze.chat_route(analyze.ChatRequest(message="hi")))
    assert "빈 응답" in str(exc.value.detail)
