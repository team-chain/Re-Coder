"""Regression coverage below the router boundary, including Bedrock request shape."""
import asyncio
import json
from types import SimpleNamespace

import pytest
import code_agent as ca
from code_output import CodeOutputError, parse_code_output, CODE_OUTPUT_SCHEMA
from llm.base import LLMError, LLMErrorType, LLMRequest
from llm.bedrock_provider import BedrockProvider
from llm.gateway_provider import GatewayProvider
from llm.provider_router import LLMProviderRouter
from llm.router import LLMRouter
from llm import breaker

VALID = {"summary":"완료", "ops":[{"action":"create","file":"app.js","content":"console.log('ok')"}]}


def generate(monkeypatch, tmp_path, responses):
    calls = []
    def call(request, **kwargs):
        calls.append(request)
        result = responses[min(len(calls)-1, len(responses)-1)]
        if isinstance(result, Exception): raise result
        return SimpleNamespace(text=result, model_used="fixture", provider="bedrock")
    monkeypatch.setattr(ca, "get_router", lambda: SimpleNamespace(call=call))
    choice = dict(ca._build_confirm_decision("게시판"), chosen_key="proceed")
    return calls, lambda: ca.generate_code("게시판", decisions=[choice], project_root=str(tmp_path))


@pytest.mark.parametrize("raw", [
    "", '{"ops":[{"file":"app.js","content":"unfinished',
    '{"raw_response":"unfinished JSON"}', '{"ops":[]}', '{"summary":"done"}',
    '{"ops":[{"file":"a.js","content":"ok"},{"file":"b.js"}]}',
    '{"ops":[{"file":"app.js","content":{"code":"not text"}}]}',
    '{"ops":[null]}', '[]', '{"ops":{}}',
])
def test_bad_generation_retries_with_schema_and_never_returns_a_partial_batch(monkeypatch, tmp_path, raw):
    calls, run = generate(monkeypatch, tmp_path, [raw, json.dumps(VALID)])
    result = run()
    assert result["ops"][0]["file"] == "app.js"
    assert len(calls) == 2
    assert all(c.json_schema == CODE_OUTPUT_SCHEMA and c.max_tokens == 8192 for c in calls)
    assert "직전 응답의 문제" in calls[1].prompt
    assert list(tmp_path.iterdir()) == [], "generation must not write files"


def test_repeated_invalid_response_is_bounded_and_does_not_expose_raw_code(monkeypatch, tmp_path):
    calls, run = generate(monkeypatch, tmp_path, ['secret-project-content'])
    with pytest.raises(RuntimeError, match="2회 시도") as error: run()
    assert len(calls) == 2
    assert 'secret-project-content' not in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_truncation_can_be_corrected_but_auth_failure_is_not_retried(monkeypatch, tmp_path):
    calls, run = generate(monkeypatch, tmp_path, [LLMError('clipped', LLMErrorType.STRUCTURED_OUTPUT), json.dumps(VALID)])
    assert run()["ops"] and len(calls) == 2
    calls, run = generate(monkeypatch, tmp_path, [LLMError('denied', LLMErrorType.ACCESS_DENIED)])
    with pytest.raises(RuntimeError, match='denied'): run()
    assert len(calls) == 1


@pytest.mark.parametrize('path', ['../escape.js','/tmp/escape.js','C:\\escape.js','folder/../../escape.js'])
def test_invalid_paths_cannot_become_valid_file_operations(path):
    with pytest.raises(CodeOutputError):
        parse_code_output(json.dumps({"ops":[{"file":path,"content":"x"}]}))


def test_fences_braces_and_old_raw_wrapper_preserve_full_code():
    code = 'const data = { value: "}\\\"" };\n// 한글'
    payload = json.dumps({"ops":[{"file":"src/app.js","content":code}]})
    for raw in [payload, '```json\n'+payload+'\n```', 'Here is the result:\n'+payload, json.dumps({'raw_response':payload})]:
        assert parse_code_output(raw)[1][0]['content'] == code
    with pytest.raises(CodeOutputError):
        parse_code_output('{"ops":['+payload+' ,unfinished')


class Client:
    def __init__(self, truncate=False): self.calls=[]; self.truncate=truncate
    def converse(self, **kwargs):
        self.calls.append(kwargs)
        # The old router drops 8192 and invokes the model at its default limit.
        if self.truncate or kwargs.get('inferenceConfig',{}).get('maxTokens') != 8192:
            return {'stopReason':'max_tokens','output':{'message':{'content':[{'text':'{"ops":[{"file":"app.js","content":"unfinished'}]}}}
        content = [{'toolUse':{'name':'output','input':VALID}}] if 'toolConfig' in kwargs else [{'text':json.dumps(VALID)}]
        return {'stopReason':'tool_use' if 'toolConfig' in kwargs else 'end_turn','output':{'message':{'content':content}}}


def provider(client):
    p = BedrockProvider.__new__(BedrockProvider)
    p._model_id = 'fixture-contract'; p.model_id = 'fixture-contract'; p._region = 'us-east-1'; p._client=client
    return p


def test_real_router_and_bedrock_forward_output_budget_temperature_and_schema():
    breaker.reset_all()
    client = Client()
    pr = LLMProviderRouter.__new__(LLMProviderRouter)
    pr._bedrock_sonnet = provider(client); pr._call_records=[]
    router = LLMRouter.__new__(LLMRouter); router._pr=pr
    response = router.call(LLMRequest(prompt='test',json_schema=CODE_OUTPUT_SCHEMA,max_tokens=8192,temperature=.2))
    assert parse_code_output(response.text)[1][0]['content'] == "console.log('ok')"
    assert client.calls[0]['inferenceConfig'] == {'maxTokens':8192,'temperature':.2}
    assert client.calls[0]['toolConfig']['tools'][0]['toolSpec']['inputSchema']['json'] == CODE_OUTPUT_SCHEMA
    assert len(client.calls) == 1


def test_bedrock_plain_and_tool_fallbacks_keep_budget_and_do_not_hide_truncation():
    for method in ['_converse_plain','_converse_tool_use','_converse_structured']:
        client = Client(); p=provider(client)
        args=([{'role':'user','content':[{'text':'test'}]}],None)
        if method != '_converse_plain': args += (CODE_OUTPUT_SCHEMA,)
        assert asyncio.run(getattr(p,method)(*args,max_tokens=8192,temperature=.2))['ops']
        assert client.calls[0]['inferenceConfig']['maxTokens'] == 8192
    client=Client(truncate=True)
    with pytest.raises(LLMError) as error:
        asyncio.run(provider(client).converse([],output_schema=CODE_OUTPUT_SCHEMA,max_tokens=8192))
    assert error.value.error_type == LLMErrorType.STRUCTURED_OUTPUT
    assert len(client.calls)==1, 'truncation must not trigger three identical provider calls'


def test_gateway_preserves_requested_limit(monkeypatch):
    p=GatewayProvider(); calls=[]
    def post(payload): calls.append(payload); return {'parsed':VALID}
    monkeypatch.setattr(p,'_post',post)
    assert asyncio.run(p.converse([],output_schema=CODE_OUTPUT_SCHEMA,max_tokens=8192,temperature=.2))['ops']
    assert calls[0]['max_tokens']==8192 and calls[0]['temperature']==.2


def test_unparseable_provider_text_is_not_serialized_as_a_success_object():
    breaker.reset_all()
    class RawProvider:
        model_id='raw-fixture'; provider_name='bedrock'
        async def converse(self,*args,**kwargs): return {'raw_response':'unfinished model text'}
    pr=LLMProviderRouter.__new__(LLMProviderRouter); pr._bedrock_sonnet=RawProvider(); pr._call_records=[]
    result=asyncio.run(pr.call_llm(LLMRequest(prompt='test')))
    assert result.text=='unfinished model text'


def test_plan_also_recovers_from_explicit_truncation(monkeypatch, tmp_path):
    calls=[]
    def call(request, **kwargs):
        calls.append(request)
        if len(calls)==1: raise LLMError('clipped',LLMErrorType.STRUCTURED_OUTPUT)
        return SimpleNamespace(text='{"decisions":[]}',model_used='fixture')
    monkeypatch.setattr(ca,'get_router',lambda:SimpleNamespace(call=call))
    result=ca.generate_plan('작은 변경',project_root=str(tmp_path))
    assert result['decisions'] and len(calls)==2


def test_truncation_is_recorded_without_model_fallback():
    breaker.reset_all()
    pr=LLMProviderRouter.__new__(LLMProviderRouter)
    pr._bedrock_sonnet=provider(Client(truncate=True)); pr._call_records=[]
    # No Gemini provider attached: this must fail with the original output reason.
    with pytest.raises(LLMError) as error:
        asyncio.run(pr.call_llm(LLMRequest(prompt='test',max_tokens=8192,json_schema=CODE_OUTPUT_SCHEMA)))
    assert error.value.error_type==LLMErrorType.STRUCTURED_OUTPUT
    assert len(pr.call_records)==1 and pr.call_records[0].output_tokens==8192
