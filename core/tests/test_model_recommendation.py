"""A안: 실제 외부 호출 없이 모델 확인·민감 고정·간단 모델 라우팅을 검증한다."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from llm import api_key_provider as akp, model_checks, task_tier
from llm.base import LLMError, LLMRequest


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'compatible'])
def test_ping_requires_one_valid_tool_call(monkeypatch, provider):
    monkeypatch.setenv('RECODER_COMPATIBLE_BASE_URL', 'https://api.example.test/v1')
    p = akp.ApiKeyProvider(provider)
    sent = []
    def response(payload):
        sent.append(payload)
        if provider == 'anthropic':
            return {'content': [{'type': 'tool_use', 'name': 'output', 'input': {'ok': True}}]}
        return {'choices': [{'message': {'tool_calls': [{'function': {'name': 'output', 'arguments': '{"ok":true}'}}]}}]}
    monkeypatch.setattr(p, '_post_once', response)
    p.ping()
    assert len(sent) == 1 and sent[0]['tools'] and sent[0]['tool_choice']
    monkeypatch.setattr(p, '_post_once', lambda payload: {'content': [{'type': 'text', 'text': '{"ok":true}'}],
        'choices': [{'message': {'content': '{"ok":true}'}}]})
    with pytest.raises(LLMError):
        p.ping()


@pytest.mark.parametrize('model,effort', [('gpt-6.1-sol','low'), ('gpt-6.1-terra','low'), ('gpt-6-luna','none'), ('gpt-6-sol','none'), ('gpt-5.6-terra','low')])
def test_reasoning_matches_chat_tool_support(model, effort):
    p = akp.ApiKeyProvider('openai', model)
    payload = p._payload([{'role': 'user', 'content': [{'text': 'x'}]}], None, {'type': 'object'}, 1024)
    assert payload['reasoning_effort'] == effort


def test_catalog_excludes_retired_and_failed_models_without_exposing_errors(monkeypatch):
    calls = []
    class Provider:
        def __init__(self, provider, model): self.model_id, self._timeout = model, 150
        def available_models(self): return ['gpt-6-astra']
        def ping(self):
            calls.append(self.model_id)
            if self.model_id in ('gpt-6.1-sol','gpt-6.1-terra','gpt-6-astra'):
                raise RuntimeError('sk-secret-error-must-not-escape')
    router = SimpleNamespace(_bedrock_sonnet=SimpleNamespace(provider_name='openai', model_id='gpt-5.6-terra'),
                             _bedrock_haiku=SimpleNamespace(model_id='gpt-6-luna'))
    assert not any(m['ready'] for m in model_checks.status(router)['catalog'])
    monkeypatch.setattr(model_checks, 'ApiKeyProvider', Provider)
    out = model_checks.check(router)
    by_id = {m['model']: m for m in out['catalog']}
    assert by_id['gpt-5.6-terra']['ready'] and by_id['gpt-6-luna']['ready']
    assert not by_id['gpt-6.1-sol']['ready']
    assert by_id['gpt-5-mini']['retirement'] == '2026-12-11' and 'gpt-5-mini' not in calls
    assert len(calls) == len(set(calls)) and out['checked_at']
    assert model_checks.status(router) == out
    assert 'secret' not in json.dumps(out)


@pytest.mark.parametrize('text', ['결제', '환불', '회원 가입', 'OAuth', '개인 정보', '전화 번호', '위치 정보'])
def test_sensitive_locks_server_roles_without_mutating_input(text):
    models = {'dev':'fast','planner':'openai','review':'fast','test':'fast'}
    assert task_tier.sensitive_role_models(text, models) == {'planner':'quality','dev':'quality','review':'fast','test':'fast'}
    assert task_tier.sensitive_role_models(text, models, False) == models
    assert models['dev'] == 'fast'


def test_generate_code_enforces_lock_before_context(monkeypatch):
    import code_agent
    seen = []
    def generate(*args, **kwargs):
        seen.append((task_tier.role_spec(), task_tier.quality_required('generate_test_draft')))
        return {'ops': []}
    monkeypatch.setattr(code_agent, '_generate_code', generate)
    code_agent.generate_code('카드 결제 추가', models={'dev':'fast'})
    code_agent.generate_code('카드 결제 추가', models={'dev':'fast'}, lock_sensitive=False)
    assert seen == [('quality', True), ('fast', False)]
    assert not task_tier.quality_required('generate_test_draft')


def test_router_fast_model_and_cost_tier_and_quality_guards(monkeypatch):
    from llm.provider_router import LLMProviderRouter
    class Provider:
        provider_name = 'bedrock'
        def __init__(self, model): self.model_id, self.calls = model, 0
        async def converse(self, *args, **kwargs):
            self.calls += 1
            return {'ok': True}
    router = LLMProviderRouter.__new__(LLMProviderRouter)
    router._bedrock_sonnet, router._bedrock_haiku = Provider('sonnet'), Provider('haiku')
    router._economy, router._extra, router._call_records = None, {}, []
    recorded=[]
    monkeypatch.setattr(router, '_record_call', lambda **kwargs: recorded.append(kwargs) or SimpleNamespace(call_id='c', estimated_cost_usd=0))
    async def call(operation):
        return await router.call_llm(LLMRequest(prompt='x'), operation=operation)
    with task_tier.role_models({'dev':'fast'}), task_tier.acting_as('dev'):
        asyncio.run(call('generate_code'))
        assert recorded[-1]['tier'] == 'economy'
        with task_tier.sensitive_request('결제'):
            asyncio.run(call('generate_test_draft'))
            assert recorded[-1]['tier'] == 'quality'
        asyncio.run(call('generate_code_fix'))
        assert recorded[-1]['tier'] == 'quality'
    with task_tier.role_models({'review':'fast'}), task_tier.acting_as('review'):
        asyncio.run(call('review_code_manifest'))
        assert recorded[-1]['tier'] == 'economy'
    router._economy = router._bedrock_haiku
    with task_tier.role_models({'test':'quality'}), task_tier.acting_as('test'):
        asyncio.run(call('generate_test_draft'))
        assert recorded[-1]['tier'] == 'quality'
    assert router._bedrock_haiku.calls == 2 and router._bedrock_sonnet.calls == 3


def test_api_key_fast_role_uses_separate_model(monkeypatch):
    from llm.provider_router import LLMProviderRouter
    monkeypatch.setenv('RECODER_AI_PROVIDER','openai')
    monkeypatch.setenv('RECODER_OPENAI_API_KEY','fixture-key')
    monkeypatch.setenv('RECODER_OPENAI_MODEL','complex-model')
    monkeypatch.setenv('RECODER_OPENAI_FAST_MODEL','simple-model')
    sent=[]
    async def converse(self, messages, **kwargs):
        sent.append(self.model_id)
        return {'ok':True}
    monkeypatch.setattr(akp.ApiKeyProvider,'converse',converse)
    router=LLMProviderRouter()
    with task_tier.role_models({'dev':'fast'}), task_tier.acting_as('dev'):
        asyncio.run(router.call_llm(LLMRequest(prompt='x'),operation='generate_code'))
    assert sent == ['simple-model']
    choices={c['spec']:c['model'] for c in router.routing_status()['choices']}
    assert choices['quality']=='complex-model' and choices['fast']=='simple-model'
    monkeypatch.delenv('RECODER_OPENAI_FAST_MODEL')
    assert akp.model_for('openai',fast=True)=='complex-model'


def test_check_endpoint_uses_probe_and_both_generate_routes_accept_lock(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import models, analyze
    called=[]
    router=SimpleNamespace(routing_status=lambda: {'catalog':[]})
    monkeypatch.setattr(models,'get_provider_router',lambda:router)
    monkeypatch.setattr(model_checks,'check',lambda r:called.append(r))
    app=FastAPI();app.include_router(models.router)
    with TestClient(app) as client:
        assert client.post('/api/models/check').json()=={'catalog':[]}
    assert called==[router]
    assert analyze.CodeGenerateRequest().lockSensitive is True
    assert analyze.CodeGenerateRequest(lockSensitive=False).lockSensitive is False
