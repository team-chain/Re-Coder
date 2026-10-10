"""Claude(Anthropic)·OpenAI API 키 경로 — AWS 없이 AI 를 쓰는 사용자.

실제 외부 API 는 부르지 않는다. 같은 모양의 응답을 주는 로컬 HTTP 서버로
요청 형태·응답 해석·오류 분류·라우터 선택을 고정한다.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from llm import api_key_provider as akp  # noqa: E402
from llm import provider_router as pr_mod  # noqa: E402
from llm.base import LLMError, LLMErrorType, LLMRequest  # noqa: E402

KEY = "sk-fixture-secret-key-0000"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


class _Server:
    def __init__(self, responder):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                status, payload = responder(body, len(outer.requests))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def env(monkeypatch):
    for name in ("RECODER_AI_PROVIDER", "RECODER_ANTHROPIC_API_KEY", "RECODER_OPENAI_API_KEY",
                 "RECODER_ANTHROPIC_MODEL", "RECODER_OPENAI_MODEL", "RECODER_ANTHROPIC_BASE_URL",
                 "RECODER_OPENAI_BASE_URL", "RECODER_LLM_GATEWAY_URL", "RECODER_STUDENT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _use(env, provider, server):
    env.setenv("RECODER_AI_PROVIDER", provider)
    env.setenv(f"RECODER_{provider.upper()}_API_KEY", KEY)
    env.setenv(f"RECODER_{provider.upper()}_BASE_URL", server.url)


def test_provider_needs_explicit_choice_and_its_own_key(env):
    env.setenv("ANTHROPIC_API_KEY", KEY)  # 다른 도구용 키가 있어도 경로를 바꾸지 않는다
    env.setenv("OPENAI_API_KEY", KEY)
    assert akp.selected_provider() == ""
    env.setenv("RECODER_AI_PROVIDER", "anthropic")
    assert akp.selected_provider() == ""
    env.setenv("RECODER_ANTHROPIC_API_KEY", KEY)
    assert akp.selected_provider() == "anthropic"
    env.setenv("RECODER_AI_PROVIDER", "unknown")
    assert akp.selected_provider() == ""


def test_anthropic_structured_output_uses_forced_tool(env):
    server = _Server(lambda body, n: (200, {
        "content": [{"type": "tool_use", "name": "output", "input": {"answer": "42"}}],
        "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 3},
    }))
    try:
        _use(env, "anthropic", server)
        provider = akp.ApiKeyProvider("anthropic")
        messages = [{"role": "user", "content": [{"text": "question"}]}]
        assert asyncio.run(provider.converse(messages, system="sys", output_schema=SCHEMA, max_tokens=500)) == {"answer": "42"}
        req = server.requests[0]
        assert req["headers"]["x-api-key"] == KEY and req["headers"]["anthropic-version"]
        body = req["body"]
        assert body["model"] == "claude-sonnet-5" and body["max_tokens"] == 500 and body["system"] == "sys"
        assert body["tool_choice"] == {"type": "tool", "name": "output"}
        assert body["tools"][0]["input_schema"] == SCHEMA
        assert "temperature" not in body
        assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "question"}]}]
    finally:
        server.close()


def test_openai_structured_output_and_reasoning_parameters(env):
    server = _Server(lambda body, n: (200, {
        "choices": [{"finish_reason": "stop", "message": {"content": None, "tool_calls": [
            {"type": "function", "function": {"name": "output", "arguments": "{\"answer\": \"ok\"}"}}]}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }))
    try:
        _use(env, "openai", server)
        response = akp.ApiKeyProvider("openai").call(LLMRequest(prompt="q", system="s", json_schema=SCHEMA, max_tokens=800))
        assert response.parsed == {"answer": "ok"} and response.provider == "openai"
        assert (response.input_tokens, response.output_tokens) == (5, 2)
        req = server.requests[0]
        assert req["headers"]["authorization"] == f"Bearer {KEY}"
        body = req["body"]
        assert body["model"] == "gpt-5-mini" and body["max_completion_tokens"] == 800
        assert body["reasoning_effort"] == "low" and "temperature" not in body and "max_tokens" not in body
        assert body["messages"][0] == {"role": "system", "content": "s"}
        assert body["tool_choice"] == {"type": "function", "function": {"name": "output"}}
    finally:
        server.close()


def test_plain_text_matches_bedrock_contract(env):
    server = _Server(lambda body, n: (200, {"choices": [{"finish_reason": "stop", "message": {"content": "hello"}}]}))
    try:
        _use(env, "openai", server)
        env.setenv("RECODER_OPENAI_MODEL", "gpt-4.1")
        provider = akp.ApiKeyProvider("openai", akp.model_for("openai"))
        result = asyncio.run(provider.converse([{"role": "user", "content": [{"text": "hi"}]}]))
        assert result == {"raw_response": "hello"}
        assert "reasoning_effort" not in server.requests[0]["body"]
    finally:
        server.close()


def test_truncated_output_is_a_generation_error(env):
    server = _Server(lambda body, n: (200, {"content": [{"type": "text", "text": "{\"a\":"}], "stop_reason": "max_tokens"}))
    try:
        _use(env, "anthropic", server)
        with pytest.raises(LLMError) as err:
            asyncio.run(akp.ApiKeyProvider("anthropic").converse([{"role": "user", "content": [{"text": "x"}]}], output_schema=SCHEMA))
        assert err.value.error_type == LLMErrorType.STRUCTURED_OUTPUT
    finally:
        server.close()


def test_rejected_key_is_not_retried_or_leaked(env):
    server = _Server(lambda body, n: (401, {"error": {"type": "authentication_error", "message": f"invalid x-api-key {KEY}"}}))
    try:
        _use(env, "anthropic", server)
        with pytest.raises(LLMError) as err:
            akp.ApiKeyProvider("anthropic").ping()
        assert err.value.error_type == LLMErrorType.ACCESS_DENIED and not err.value.retryable
        assert KEY not in str(err.value)
        assert len(server.requests) == 1
    finally:
        server.close()


def test_unsupported_reasoning_parameter_is_dropped_once(env):
    def responder(body, n):
        if "reasoning_effort" in body:
            return 400, {"error": {"message": "Unsupported parameter: 'reasoning_effort'", "code": "unsupported_parameter"}}
        return 200, {"choices": [{"finish_reason": "stop", "message": {"content": "{\"answer\": \"x\"}"}}]}
    server = _Server(responder)
    try:
        _use(env, "openai", server)
        assert asyncio.run(akp.ApiKeyProvider("openai").converse([{"role": "user", "content": [{"text": "x"}]}])) == {"answer": "x"}
        assert len(server.requests) == 2 and "reasoning_effort" not in server.requests[1]["body"]
    finally:
        server.close()


def test_temporary_server_error_is_retried_once(env):
    server = _Server(lambda body, n: (529, {"error": {"message": "overloaded"}}) if n == 1 else (200, {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}))
    try:
        _use(env, "anthropic", server)
        assert asyncio.run(akp.ApiKeyProvider("anthropic").converse([{"role": "user", "content": [{"text": "x"}]}])) == {"raw_response": "ok"}
        assert len(server.requests) == 2
    finally:
        server.close()


def test_router_uses_selected_key_provider_and_keeps_bedrock_otherwise(env):
    router = pr_mod.LLMProviderRouter()
    assert router._bedrock_sonnet.provider_name == "bedrock"
    env.setenv("RECODER_AI_PROVIDER", "openai")
    env.setenv("RECODER_OPENAI_API_KEY", KEY)
    router = pr_mod.LLMProviderRouter()
    assert router._bedrock_sonnet.provider_name == "openai" and router._bedrock_haiku.provider_name == "openai"
    # 명시적 선택이 게이트웨이보다 우선한다.
    env.setenv("RECODER_LLM_GATEWAY_URL", "https://gateway.example.test")
    env.setenv("RECODER_STUDENT_TOKEN", "rcdr_a_b")
    assert pr_mod.LLMProviderRouter()._bedrock_sonnet.provider_name == "openai"


def test_ai_ready_diagnostics_ping_the_selected_provider(env):
    import first_run
    server = _Server(lambda body, n: (200, {"content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn"}))
    try:
        _use(env, "anthropic", server)
        status, model, region, provider, cross = first_run._check_ai_ready_sync()
        assert (status, model, provider) == (first_run.ReadyStatus.OK, "claude-sonnet-5", "anthropic")
        assert server.requests[0]["body"]["max_tokens"] == 16
    finally:
        server.close()
    bad = _Server(lambda body, n: (401, {"error": {"message": "bad key"}}))
    try:
        env.setenv("RECODER_ANTHROPIC_BASE_URL", bad.url)
        status, *_rest, provider, _cross = first_run._check_ai_ready_sync()
        assert status == first_run.ReadyStatus.FAIL and provider == "anthropic"
    finally:
        bad.close()


@pytest.mark.parametrize("provider,status,payload", [
    ("anthropic", 400, {"type": "error", "error": {"type": "invalid_request_error",
                                                   "message": "Your credit balance is too low to access the Anthropic API."}}),
    ("openai", 429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota."}}),
    ("openai", 402, {"error": {"message": "Payment required"}}),
])
def test_credit_exhaustion_is_reported_as_quota_not_bad_request(env, provider, status, payload):
    from llm.failure import public_ai_failure_reason
    server = _Server(lambda body, n: (status, payload))
    try:
        _use(env, provider, server)
        with pytest.raises(LLMError) as err:
            akp.ApiKeyProvider(provider).ping()
        assert err.value.error_type == LLMErrorType.QUOTA_EXCEEDED and not err.value.retryable
        assert "크레딧" in str(err.value)
        assert "크레딧" in public_ai_failure_reason(err.value)
        assert len(server.requests) == 1
    finally:
        server.close()


def test_글자_그대로_받기는_JSON_을_뽑지_않고_끊겨도_받은_만큼_돌려준다(env):
    """긴 파일을 이어 받는 생성 엔진용 — package.json 내용이 dict 로 바뀌거나 끊김이 오류가 되면 안 된다."""
    pkg = '{"name": "shop",\n  "scripts": {"start": "node s.js"}}'
    server = _Server(lambda body, n: (200, {"content": [{"type": "text", "text": pkg}], "stop_reason": "max_tokens"}))
    try:
        _use(env, "anthropic", server)
        out = asyncio.run(akp.ApiKeyProvider("anthropic").converse([{"role": "user", "content": [{"text": "x"}]}], raw=True))
        assert out == {"text": pkg, "truncated": True}
        assert "tools" not in server.requests[0]["body"]
    finally:
        server.close()
    server = _Server(lambda body, n: (200, {"choices": [{"finish_reason": "stop", "message": {"content": "a{}\n"}}]}))
    try:
        _use(env, "openai", server)
        env.setenv("RECODER_OPENAI_MODEL", "gpt-4.1")
        out = asyncio.run(akp.ApiKeyProvider("openai", akp.model_for("openai")).converse([{"role": "user", "content": [{"text": "x"}]}], raw=True))
        assert out == {"text": "a{}\n", "truncated": False}
    finally:
        server.close()


def test_라우터는_글자_그대로_요청이면_원문과_끊김_여부를_돌려준다(env):
    pkg = '{"name": "shop"}'
    server = _Server(lambda body, n: (200, {"content": [{"type": "text", "text": pkg}], "stop_reason": "max_tokens"}))
    try:
        _use(env, "anthropic", server)
        router = pr_mod.LLMProviderRouter()
        resp = asyncio.run(router.call_llm(LLMRequest(prompt="x", max_tokens=8192, raw_text=True), agent="t", operation="o"))
        assert resp.text == pkg and resp.metadata["truncated"] is True
    finally:
        server.close()
