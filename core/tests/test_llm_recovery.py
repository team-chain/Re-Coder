"""User's plain board request must survive a transient Bedrock failure."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError
from llm import breaker
from llm.base import LLMError, LLMErrorType, LLMRequest
from llm.bedrock_provider import BedrockProvider
from llm.gemini_provider import GeminiProvider
from llm.provider_router import LLMProviderRouter


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    breaker.reset_all()
    monkeypatch.setattr('llm.bedrock_provider.asyncio.sleep', AsyncMock())
    yield
    breaker.reset_all()


def fixture(code='ServiceUnavailableException', failures=1):
    calls=[]
    def converse(**kwargs):
        calls.append(kwargs)
        if len(calls)<=failures:
            raise ClientError({'Error':{'Code':code,'Message':'Bedrock temporarily unavailable'}},'Converse')
        return {'stopReason':'end_turn','output':{'message':{'content':[{'text':'{"decisions":[]}'}]}}}
    p=BedrockProvider.__new__(BedrockProvider)
    p.model_id=p._model_id='recovery-test'; p._client=SimpleNamespace(converse=converse)
    g=GeminiProvider.__new__(GeminiProvider); g._api_key=''; g._model=None
    g.generate=AsyncMock(side_effect=AssertionError('An unconfigured Gemini must never be called'))
    r=LLMProviderRouter.__new__(LLMProviderRouter)
    r._bedrock_sonnet=r._bedrock_haiku=p; r._gemini=g; r._call_records=[]
    return r,calls,g


@pytest.mark.parametrize('instruction',['그냥 게시판 하나 만들어줘','게시판 디자인 예쁘게 해서 만들어줘'])
def test_both_phrasings_recover_from_transient_bedrock_error(instruction):
    router,calls,gemini=fixture()
    response=asyncio.run(router.call_llm(LLMRequest(prompt=instruction)))
    assert response.provider=='bedrock' and response.parsed=={'decisions':[]}
    assert len(calls)==2 and calls[0]==calls[1]
    assert calls[0]['messages'][0]['content'][0]['text']==instruction
    gemini.generate.assert_not_awaited()


@pytest.mark.parametrize('entry',['primary','fast','llm'])
def test_outage_preserves_original_failure_without_gemini_configuration_error(entry):
    router,calls,gemini=fixture(failures=99)
    operation=(router.call_llm(LLMRequest(prompt='test',json_schema={'type':'object'})) if entry=='llm'
               else getattr(router,'call_'+entry)('test',schema={'type':'object'}))
    with pytest.raises(LLMError) as failure:asyncio.run(operation)
    assert failure.value.error_type==LLMErrorType.SERVICE_ERROR
    assert 'ServiceUnavailableException' in str(failure.value) and 'Gemini' not in str(failure.value)
    assert len(calls)==2, 'Do not repeat an outage for each output format'
    gemini.generate.assert_not_awaited()


def test_authentication_failure_does_not_retry_or_change_output_formats():
    router,calls,gemini=fixture(code='AccessDeniedException',failures=99)
    with pytest.raises(LLMError) as failure:
        asyncio.run(router.call_llm(LLMRequest(prompt='test',json_schema={'type':'object'})))
    assert failure.value.error_type==LLMErrorType.ACCESS_DENIED and len(calls)==1
    gemini.generate.assert_not_awaited()


def test_configured_fallback_is_still_available():
    router,calls,gemini=fixture(failures=99)
    gemini._api_key='fixture'; gemini._model=object()
    gemini.generate=AsyncMock(return_value={'text':'fallback result'})
    response=asyncio.run(router.call_llm(LLMRequest(prompt='test')))
    assert response.provider=='gemini' and response.text=='fallback result'
    assert len(calls)==2
