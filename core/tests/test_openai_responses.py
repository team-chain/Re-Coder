"""Responses-only models must pass the same real tool-output contract as chat models."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from llm.api_key_provider import ApiKeyProvider
from llm.base import LLMError, LLMErrorType, LLMRequest
from llm import model_checks


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
RESPONSE = {"status": "completed", "output": [
    {"type": "reasoning", "summary": []},
    {"type": "function_call", "name": "output", "arguments": '{"ok":true}', "call_id": "test"}],
    "usage": {"input_tokens": 14, "output_tokens": 8}}


@pytest.mark.parametrize("model", ["gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-terra"])
def test_responses_request_and_function_output(monkeypatch, model):
    p = ApiKeyProvider("openai", model)
    sent = []
    def post(payload):
        sent.append(payload)
        return RESPONSE
    monkeypatch.setattr(p, "_post_once", post)
    p.ping()
    result = p.call(LLMRequest(prompt="test", system="instruction", json_schema=SCHEMA, max_tokens=2000))
    assert p._endpoint == "https://api.openai.com/v1/responses"
    payload = sent[-1]
    assert payload["instructions"] == "instruction" and payload["store"] is False
    assert payload["tool_choice"] == {"type": "function", "name": "output"}
    assert payload["tools"][0]["parameters"] == SCHEMA and payload["tools"][0]["strict"] is False
    assert "messages" not in payload and payload["max_output_tokens"] == 2000
    assert result.parsed == {"ok": True} and (result.input_tokens, result.output_tokens) == (14, 8)


def test_responses_raw_output_and_usage(monkeypatch):
    p = ApiKeyProvider("openai", "gpt-6.1-sol")
    monkeypatch.setattr(p, "_post_once", lambda payload: {"status": "incomplete", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "content": [{"type": "output_text", "text": "partial"}]}],
        "usage": {"input_tokens": 7, "output_tokens": 4}})
    usage = {}
    out = asyncio.run(p.converse([{"role": "user", "content": "test"}], raw=True, usage=usage))
    assert out == {"text": "partial", "truncated": True}
    assert usage == {"input_tokens": 7, "output_tokens": 4}
    with pytest.raises(LLMError):
        p.ping()


@pytest.mark.parametrize("output", [
    [{"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}],
    [{"type": "function_call", "name": "wrong", "arguments": '{"ok":true}'}],
    [{"type": "function_call", "name": "output", "arguments": '{invalid'}],
])
def test_ping_does_not_accept_text_or_invalid_tools(monkeypatch, output):
    p = ApiKeyProvider("openai", "gpt-6-astra")
    monkeypatch.setattr(p, "_post_once", lambda payload: {"status": "completed", "output": output})
    with pytest.raises(LLMError):
        p.ping()


def test_check_exposes_only_allowlisted_failure_category(monkeypatch):
    class Provider:
        def __init__(self, *args): self._timeout = 150
        def available_models(self): return []
        def ping(self): raise LLMError("private-provider-secret", LLMErrorType.VALIDATION_ERROR)
    monkeypatch.setattr(model_checks, "ApiKeyProvider", Provider)
    router = SimpleNamespace(_bedrock_sonnet=SimpleNamespace(provider_name="openai", model_id="gpt-6.1-terra"),
                             _bedrock_haiku=SimpleNamespace(model_id="gpt-6-luna"))
    result = model_checks.check(router)
    assert result["catalog"][0]["reason"] == "API 요청 호환 오류"
    assert "private-provider-secret" not in json.dumps(result)
