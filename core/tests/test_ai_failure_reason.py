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
# 진단(AI 연결 확인) — 실패 이유가 화면으로 간다. 예전에는 core.log 에만 있었다(실기기 2026-11-15).
# ---------------------------------------------------------------------------

import first_run  # noqa: E402
from llm import api_key_provider  # noqa: E402


class _Broke:
    def __init__(self, *a, **k):
        self._timeout = 60
        self.model_id = "claude-x"

    def ping(self):
        raise RuntimeError("Claude API 크레딧·사용 한도가 부족합니다. Claude API 콘솔에서 결제·한도를 확인하세요: "
                           "Your credit balance is too low (key sk-ant-api03-abcdefghijklmnopqrstuvwxyz)")


def test_chosen_api_key_failure_reason_reaches_diagnostics(monkeypatch):
    monkeypatch.setattr(api_key_provider, "selected_provider", lambda: "anthropic")
    monkeypatch.setattr(api_key_provider, "ApiKeyProvider", _Broke)
    monkeypatch.setattr(first_run, "check_core_ready", lambda: (first_run.ReadyStatus.OK, []))
    monkeypatch.setattr(first_run, "check_docker_ready", lambda: (first_run.ReadyStatus.OK, "Docker"))
    monkeypatch.setattr(first_run, "check_aws_deploy_ready", lambda: (first_run.ReadyStatus.OK, []))
    monkeypatch.setattr(first_run, "save_diagnostics", lambda result: None)
    for name in ("check_ops_ready",):
        if hasattr(first_run, name):
            monkeypatch.setattr(first_run, name, lambda: (first_run.ReadyStatus.OK, []))
    result = asyncio.run(first_run.run_diagnostics())
    assert result.ai_ready != "ok"
    reason = result.details["ai_failure"]
    assert any("크레딧" in i for i in result.issues)
    assert not any("Bedrock or Gemini" in i for i in result.issues)
    assert "크레딧" in reason and "credit balance" not in reason  # 공급자 원문은 내보내지 않는다
    assert "abcdefghijklmnop" not in reason


def test_success_clears_previous_reason(monkeypatch):
    first_run._last_ai_failure = "old"

    class _Ok(_Broke):
        def ping(self):
            return None
    monkeypatch.setattr(api_key_provider, "selected_provider", lambda: "anthropic")
    monkeypatch.setattr(api_key_provider, "ApiKeyProvider", _Ok)
    assert first_run._check_ai_ready_sync()[0] == first_run.ReadyStatus.OK
    assert first_run._last_ai_failure == ""
