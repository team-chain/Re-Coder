import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from api.routes import security_checks as route
from llm import provider_router


def body(**changes):
    return route.AuditStepRequest(**{'role': 'hunter', 'instructions': 'Return source findings.',
                                    'data': {'files': [{'path': 'app.ts', 'text': 'const x = 1'}]}, **changes})


def install(monkeypatch, result=None, error=None):
    calls = []
    async def call(request, **options):
        calls.append((request, options))
        if error:
            raise error
        return SimpleNamespace(parsed=result, text='', metadata={}, provider='existing-provider',
                               model_used='configured-model', input_tokens=17, output_tokens=9)
    monkeypatch.setattr(provider_router, 'get_provider_router', lambda: SimpleNamespace(call_llm=call))
    return calls


def test_audit_uses_existing_develop_router_without_new_auth_or_cloud_setup(monkeypatch):
    calls = install(monkeypatch, {'findings': []})
    result = asyncio.run(route.audit_step(body()))
    assert result['provider'] == 'existing-provider'
    assert result['model'] == 'configured-model'
    request, options = calls[0]
    assert options == {'agent': 'security-audit', 'operation': 'security_audit', 'prefer': 'primary'}
    assert request.max_tokens == 4000
    assert request.json_schema['required'] == ['findings']
    assert request.json_schema['properties']['findings']['type'] == 'array'
    assert 'evidence' in request.json_schema['$defs']['AuditFinding']['required']
    assert 'Never run tools' in request.system
    assert result['inputTokens'] == 17


def test_known_secrets_are_redacted_before_the_existing_model_is_called(monkeypatch):
    calls = install(monkeypatch, {'findings': []})
    secret = 'sk_test_' + 'a' * 28
    asyncio.run(route.audit_step(body(data={'files': [{'path': 'app.ts', 'text': secret}]})))
    assert secret not in calls[0][0].prompt


@pytest.mark.parametrize('result', [[], 'invalid', {}, {'findings': 'none'}, {'findings': [{}]}, {'x': 'a' * (130 * 1024)}])
def test_incomplete_or_oversized_results_are_not_success(monkeypatch, result):
    install(monkeypatch, result)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(route.audit_step(body()))
    assert caught.value.status_code == 502


def test_existing_provider_failure_is_visible_and_does_not_leak_secret(monkeypatch):
    calls = install(monkeypatch, error=RuntimeError('private-secret-value'))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(route.audit_step(body()))
    assert len(calls) == 1 and 'private-secret-value' not in str(caught.value.detail)
    assert 'Develop' in caught.value.detail


def test_audit_timeout_and_oversize_stop_without_success(monkeypatch):
    calls = install(monkeypatch, error=asyncio.TimeoutError())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(route.audit_step(body()))
    assert caught.value.status_code == 504
    calls.clear()
    with pytest.raises(HTTPException) as caught:
        asyncio.run(route.audit_step(body(data={'text': 'a' * (513 * 1024)})))
    assert caught.value.status_code == 413 and not calls


def test_unknown_agent_role_is_rejected_before_model_use():
    with pytest.raises(ValidationError):
        body(role='shell')


@pytest.mark.parametrize('role,result,required', [
    ('recon', {'boundaries': [], 'entrypoints': [], 'unknowns': []}, ['boundaries', 'entrypoints', 'unknowns']),
    ('critic', {'findings': []}, ['findings']),
    ('verifier', {'supported': False, 'reason': 'Missing caller context'}, ['supported', 'reason']),
    ('final-verifier', {'supported': True, 'reason': 'Source confirms the boundary'}, ['supported', 'reason']),
])
def test_each_stage_requests_and_validates_its_structured_contract(monkeypatch, role, result, required):
    calls = install(monkeypatch, result)
    assert asyncio.run(route.audit_step(body(role=role)))['result'] == result
    assert calls[0][0].json_schema['required'] == required


def test_text_boolean_cannot_confirm_a_finding(monkeypatch):
    install(monkeypatch, {'supported': 'true', 'reason': 'model returned the wrong type'})
    with pytest.raises(HTTPException) as caught:
        asyncio.run(route.audit_step(body(role='verifier')))
    assert caught.value.status_code == 502
