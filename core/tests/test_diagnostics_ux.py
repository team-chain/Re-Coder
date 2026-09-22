"""Readiness checks use configured routing candidates and honest credential labels."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import first_run
from schemas import ReadyStatus


def test_configured_model_is_probed_before_any_catalog_model(monkeypatch):
    monkeypatch.setenv('BEDROCK_PRIMARY_MODEL_IDENTIFIER', 'global.configured-model')
    monkeypatch.setattr(first_run, '_detect_bedrock_region', lambda: 'ap-northeast-2')
    runtime = Mock()
    session = Mock()
    session.get_credentials.return_value = object()
    session.client.return_value = runtime
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(Session=lambda: session))
    status, model, region, provider, cross = asyncio.run(first_run.check_ai_ready())
    assert (status, model, region, provider, cross) == (ReadyStatus.OK, 'global.configured-model', 'ap-northeast-2', 'bedrock', True)
    assert runtime.converse.call_args.kwargs['modelId'] == 'global.configured-model'
    runtime.list_foundation_models.assert_not_called()
    assert runtime.converse.call_count == 1


def test_default_model_matches_provider_and_raw_model_is_not_cross_region(monkeypatch):
    from llm import bedrock_provider
    monkeypatch.delenv('BEDROCK_PRIMARY_MODEL_IDENTIFIER', raising=False)
    monkeypatch.setattr(bedrock_provider, 'DEFAULT_PRIMARY_MODEL', 'test.raw-model')
    monkeypatch.setattr(first_run, '_detect_bedrock_region', lambda: 'ap-northeast-2')
    runtime = Mock()
    session = Mock()
    session.get_credentials.return_value = object()
    session.client.return_value = runtime
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(Session=lambda: session))
    status, model, _, _, cross = asyncio.run(first_run.check_ai_ready())
    assert status == ReadyStatus.OK
    assert model == 'test.raw-model'
    assert cross is False


def test_fallback_reports_the_model_that_actually_answered(monkeypatch):
    monkeypatch.setenv('BEDROCK_PRIMARY_MODEL_IDENTIFIER', 'global.unavailable')
    monkeypatch.setattr(first_run, '_detect_bedrock_region', lambda: 'ap-northeast-2')
    from llm import bedrock_provider
    monkeypatch.setattr(bedrock_provider, 'PRIMARY_MODELS', ['global.unavailable', 'apac.available'])
    monkeypatch.setattr(bedrock_provider, 'FAST_MODELS', ['apac.available'])
    runtime = Mock()
    runtime.converse.side_effect = lambda **kw: {} if kw['modelId'] == 'apac.available' else (_ for _ in ()).throw(RuntimeError('unavailable'))
    session = Mock()
    session.get_credentials.return_value = object()
    session.client.return_value = runtime
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(Session=lambda: session))
    result = asyncio.run(first_run.check_ai_ready())
    assert result[1] == 'apac.available'
    assert [c.kwargs['modelId'] for c in runtime.converse.call_args_list] == ['global.unavailable', 'apac.available']


@pytest.mark.parametrize('storage,expected', [('assumed_role',''),('env','1234')])
def test_role_status_hides_temporary_key_suffix(monkeypatch, storage, expected):
    from api.routes import aws
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'FAKE_TEMPORARY1234')
    monkeypatch.setattr(aws, '_load_into_process_if_needed', lambda: None)
    monkeypatch.setattr(aws, '_refresh_role_credentials', lambda: None)
    monkeypatch.setattr(aws, '_detect_credential_source', lambda: (storage, 'default'))
    monkeypatch.setattr(aws, '_call_sts_get_caller_identity', lambda **kw: {'account':'123456789012','arn':'arn:aws:iam::123456789012:user/test','user_id':'test'})
    monkeypatch.setattr(aws, '_role_status_fields', lambda: {})
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace())
    status = asyncio.run(aws.get_aws_status())
    assert status.ready
    assert status.access_key_last4 == expected


def test_diagnostics_no_longer_requests_unused_model_catalog_permission():
    import aws_policy
    assert 'bedrock:ListFoundationModels' not in aws_policy.used_actions(aws_policy.build_policy(['bedrock']))
