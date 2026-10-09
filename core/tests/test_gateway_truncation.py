"""게이트웨이는 출력 토큰을 조용히 깎고 stop_reason 을 안 주었다 — 잘림을 알아채야 나눠서 만든다."""
from __future__ import annotations

import pytest

from llm.base import LLMError, LLMErrorType
from llm.gateway_provider import GatewayProvider


def test_출력이_게이트웨이_상한에_닿으면_잘린_것으로_본다(monkeypatch):
    monkeypatch.delenv("RECODER_GATEWAY_MAX_OUTPUT", raising=False)
    with pytest.raises(LLMError) as err:
        GatewayProvider._require_complete({"text": '{"ops":[{"content":"...', "output_tokens": 4096}, 8192)
    assert err.value.error_type == LLMErrorType.STRUCTURED_OUTPUT


def test_상한보다_적게_썼거나_정상_종료면_통과(monkeypatch):
    monkeypatch.delenv("RECODER_GATEWAY_MAX_OUTPUT", raising=False)
    GatewayProvider._require_complete({"output_tokens": 1200}, 8192)
    GatewayProvider._require_complete({"output_tokens": 4096, "stop_reason": "end_turn"}, 8192)
    GatewayProvider._require_complete({"output_tokens": 4096})  # 요청 상한을 모르면 추측하지 않는다


def test_stop_reason_이_오면_그대로_따른다_and_상한은_설정으로(monkeypatch):
    with pytest.raises(LLMError):
        GatewayProvider._require_complete({"output_tokens": 10, "stop_reason": "max_tokens"}, 8192)
    monkeypatch.setenv("RECODER_GATEWAY_MAX_OUTPUT", "8192")
    GatewayProvider._require_complete({"output_tokens": 4096}, 8192)
    with pytest.raises(LLMError):
        GatewayProvider._require_complete({"output_tokens": 8190}, 8192)


def test_요청한_상한이_더_작으면_그_상한을_쓴다(monkeypatch):
    monkeypatch.delenv("RECODER_GATEWAY_MAX_OUTPUT", raising=False)
    with pytest.raises(LLMError):
        GatewayProvider._require_complete({"output_tokens": 2048}, 2048)


def test_연결_확인_ping_은_잘려도_정상이다(monkeypatch):
    """AI 연결 확인은 max_tokens=1 로 부른다 — 잘림으로 보면 모든 게이트웨이 사용자가 'AI 연결 필요'가 된다."""
    monkeypatch.delenv("RECODER_GATEWAY_MAX_OUTPUT", raising=False)
    GatewayProvider._require_complete({"output_tokens": 1}, 1)
    GatewayProvider._require_complete({"output_tokens": 1, "stop_reason": "max_tokens"}, 1)
    # 작은 분류 호출(256)은 stop_reason 이 없으면 추측하지 않는다.
    GatewayProvider._require_complete({"output_tokens": 256}, 256)


def test_게이트웨이_ping_으로_AI_준비_확인이_통과한다(monkeypatch):
    import first_run
    monkeypatch.setenv("RECODER_LLM_GATEWAY_URL", "http://gw.invalid")
    monkeypatch.setenv("RECODER_STUDENT_TOKEN", "t")
    for k in ("RECODER_AI_PROVIDER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(GatewayProvider, "_post", lambda self, payload: {"text": "p", "output_tokens": 1, "stop_reason": "max_tokens", "model_used": "m"})
    status, *_ = first_run._check_ai_ready_sync()
    assert str(getattr(status, "value", status)).lower() == "ok"


def test_평문_응답은_예전처럼_잘려도_돌려준다(monkeypatch):
    """배포·운영 요약 같은 평문 호출이 갑자기 실패하지 않게 — 잘림 오류는 JSON 구조를 요구한 호출에만."""
    monkeypatch.delenv("RECODER_GATEWAY_MAX_OUTPUT", raising=False)
    GatewayProvider._require_complete({"output_tokens": 4096}, 8192, structured=False)
    GatewayProvider._require_complete({"output_tokens": 1024, "stop_reason": "max_tokens"}, 1024, structured=False)
    with pytest.raises(LLMError):
        GatewayProvider._require_complete({"output_tokens": 4096}, 8192, structured=True)
