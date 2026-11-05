"""Existing AWS connection reuse; no real AWS credentials, provisioning or network."""
import base64
import io
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from botocore.credentials import Credentials
from api.routes import aws
import managed_aws

ACCOUNT, REGION = '123456789012', 'us-east-1'
ID = 'a' * 32


def request(**changes):
    data = dict(account=ACCOUNT, region=REGION, connection_id=ID, service='cloudformation',
                content_type='application/x-www-form-urlencoded',
                body_base64=base64.b64encode(urlencode({'Action': 'DescribeStacks', 'Version': '2010-05-15', 'StackName': 'recoder-security-v1'}).encode()).decode())
    data.update(changes)
    return managed_aws.ManagedAwsRequest(**data)


@pytest.fixture
def existing(monkeypatch):
    monkeypatch.setattr(aws, '_load_into_process_if_needed', lambda: None)
    monkeypatch.setattr(aws, '_refresh_role_credentials', lambda: None)
    monkeypatch.setattr(aws, '_user_disconnected', lambda: False)
    monkeypatch.setattr(aws, '_managed_connection_id', ID)
    monkeypatch.setattr(aws, '_managed_identities', {})
    monkeypatch.setattr(aws.aws_browser, 'validate_account', lambda _: None)
    monkeypatch.setenv('AWS_REGION', REGION)
    monkeypatch.setattr(aws, '_detect_credential_source', lambda: ('aws_credentials_file', 'already-connected'))
    state = {'account': ACCOUNT, 'profile': None, 'frozen': Credentials('existing-key', 'never-export-secret', 'existing-session').get_frozen_credentials()}
    class Session:
        def get_credentials(self):
            return SimpleNamespace(get_frozen_credentials=lambda: state['frozen'])
        def client(self, service, **kwargs):
            assert service == 'sts'
            assert kwargs['aws_access_key_id'] == state['frozen'].access_key
            return SimpleNamespace(get_caller_identity=lambda: {'Account': state['account'], 'Arn': f'arn:aws:iam::{state["account"]}:user/existing'})
    def session(**kwargs):
        state['profile'] = kwargs['profile']
        return Session()
    monkeypatch.setattr(aws, '_build_boto3_session', session)
    app = FastAPI(); app.include_router(aws.router)
    return state, TestClient(app)


@pytest.mark.parametrize('storage,profile,expected', [('recoder', '', None), ('aws_credentials_file', 'saved-sso', 'saved-sso'), ('assumed_role', 'base-user', None), ('browser_login', '', None)])
def test_reuses_all_existing_connections_without_exporting_credentials(existing, monkeypatch, storage, profile, expected):
    state, client = existing
    monkeypatch.setattr(aws, '_detect_credential_source', lambda: (storage, profile))
    response = client.get('/api/aws/managed/session')
    assert response.status_code == 200
    assert response.json() == {'connected': True, 'account': ACCOUNT, 'region': REGION, 'connection_id': ID}
    assert state['profile'] == expected
    assert 'existing-key' not in response.text and 'never-export-secret' not in response.text and 'existing-session' not in response.text


def test_disconnect_does_not_fall_back_to_ambient_credentials(existing, monkeypatch):
    _, client = existing
    monkeypatch.setattr(aws, '_user_disconnected', lambda: True)
    monkeypatch.setattr(aws, '_build_boto3_session', lambda **_: pytest.fail('must not fall back'))
    assert client.get('/api/aws/managed/session').json() == {'connected': False}
    assert client.post('/api/aws/managed/request', json=request().model_dump()).status_code == 409


@pytest.mark.parametrize('change', ['account', 'region', 'connection'])
def test_changed_connection_never_dispatches_an_approved_request(existing, monkeypatch, change):
    state, client = existing
    if change == 'account': state['account'] = '999999999999'
    elif change == 'region': monkeypatch.setenv('AWS_REGION', 'ap-northeast-2')
    else: monkeypatch.setattr(aws, '_managed_connection_id', 'b' * 32)
    monkeypatch.setattr(managed_aws, 'execute', lambda *a: pytest.fail('request escaped changed connection'))
    assert client.post('/api/aws/managed/request', json=request().model_dump()).status_code == 409


def test_role_refresh_uses_new_credentials_and_revalidates_the_same_account(existing, monkeypatch):
    state, client = existing
    first = client.get('/api/aws/managed/session').json()
    state['frozen'] = Credentials('refreshed-key', 'refreshed-secret', 'new-token').get_frozen_credentials()
    seen = []
    monkeypatch.setattr(managed_aws, 'execute', lambda req, body, creds: seen.append(creds.access_key) or {'status': 200, 'body_base64': ''})
    assert client.post('/api/aws/managed/request', json=request().model_dump()).status_code == 200
    assert seen == ['refreshed-key']
    assert client.get('/api/aws/managed/session').json() == first


@pytest.mark.parametrize('kind', ['security', 'build', 'container'])
def test_runner_requests_allow_only_current_account_resources(kind):
    run = 'rc-rt-' + 'a' * 24
    req = request(service='s3', method='PUT', content_type='application/octet-stream', bucket=f'recoder-{kind}-{ACCOUNT}-{REGION}', key=f'sources/{run}.zip', body_base64=base64.b64encode(b'zip').decode())
    assert managed_aws.validate_request(req) == b'zip'
    for changes in ({'bucket': f'recoder-{kind}-999999999999-{REGION}'}, {'key': f'reports/{run}/report.json'}, {'key': '../outside'}, {'method': 'GET'}):
        with pytest.raises(HTTPException): managed_aws.validate_request(req.model_copy(update=changes))


def test_does_not_offer_an_arbitrary_aws_proxy():
    bad = request(body_base64=base64.b64encode(urlencode({'Action': 'DeleteStack', 'Version': '2010-05-15', 'StackName': 'production'}).encode()).decode())
    with pytest.raises(HTTPException): managed_aws.validate_request(bad)
    for target, value in [('CodeBuild_20161006.StartBuild', {'projectName': 'production'}), ('DynamoDB_20120810.PutItem', {'TableName': 'customers', 'Item': {'pk': {'S': 'active'}}})]:
        service = 'codebuild' if target.startswith('CodeBuild') else 'dynamodb'
        req = request(service=service, target=target, content_type='application/x-amz-json-1.1' if service == 'codebuild' else 'application/x-amz-json-1.0', body_base64=base64.b64encode(json.dumps(value).encode()).decode())
        with pytest.raises(HTTPException): managed_aws.validate_request(req)


@pytest.mark.parametrize('token', [None, 'temporary-session'])
def test_request_signing_preserves_xml_and_binary_and_never_returns_keys(token):
    calls = []
    def send(req):
        calls.append(req)
        return SimpleNamespace(status_code=200, raw=io.BytesIO(b'\x00\xffresult'))
    req = request()
    result = managed_aws.execute(req, managed_aws.validate_request(req), Credentials('existing-key', 'private-secret', token).get_frozen_credentials(), SimpleNamespace(send=send))
    assert base64.b64decode(result['body_base64']) == b'\x00\xffresult'
    assert calls[0].url == f'https://cloudformation.{REGION}.amazonaws.com/'
    assert 'Credential=existing-key/' in calls[0].headers['Authorization']
    assert ('X-Amz-Security-Token' in calls[0].headers) == bool(token)
    assert 'private-secret' not in json.dumps(result) and 'existing-key' not in json.dumps(result)


def test_access_denied_is_permission_error_not_relogin_and_no_automatic_retry():
    calls = []
    def denied(req):
        calls.append(req)
        return SimpleNamespace(status_code=403, raw=io.BytesIO(b'{"__type":"AccessDeniedException","message":"sensitive provider detail"}'))
    req = request()
    result = managed_aws.execute(req, managed_aws.validate_request(req), Credentials('k', 's').get_frozen_credentials(), SimpleNamespace(send=denied))
    assert result == {'status': 403, 'error_code': 'AccessDeniedException'} and len(calls) == 1
    def fail(req):
        calls.append(req); raise OSError('private key details must not be forwarded')
    with pytest.raises(HTTPException) as error:
        managed_aws.execute(req, managed_aws.validate_request(req), Credentials('k', 's').get_frozen_credentials(), SimpleNamespace(send=fail))
    assert error.value.status_code == 502 and 'private' not in str(error.value.detail) and len(calls) == 2


@pytest.mark.parametrize('kind', ['security', 'build', 'container'])
def test_real_job_dispatch_fields_are_accepted_but_buildspec_or_destination_overrides_are_not(kind):
    run = 'rc-rt-' + 'a' * 24
    bucket = f'recoder-{kind}-{ACCOUNT}-{REGION}'
    values = {'projectName': f'recoder-{kind}-v1', 'idempotencyToken': run, 'sourceLocationOverride': f'{bucket}/sources/{run}.zip', 'sourceTypeOverride': 'S3',
              'environmentVariablesOverride': [{'name': k, 'value': v, 'type': 'PLAINTEXT'} for k, v in [('RECODER_RUN_ID', run), ('RECODER_SOURCE_SHA256', 'b' * 64), ('RECODER_REPORT_BUCKET', bucket)]],
              'artifactsOverride': {'type': 'S3', 'location': bucket, 'path': 'reports', 'namespaceType': 'NONE', 'name': run, 'packaging': 'NONE', 'encryptionDisabled': False}}
    def payload(v):
        return request(service='codebuild', target='CodeBuild_20161006.StartBuild', content_type='application/x-amz-json-1.1', body_base64=base64.b64encode(json.dumps(v).encode()).decode())
    assert json.loads(managed_aws.validate_request(payload(values))) == values
    with pytest.raises(HTTPException): managed_aws.validate_request(payload({**values, 'buildspecOverride': 'unreviewed commands'}))
    with pytest.raises(HTTPException): managed_aws.validate_request(payload({**values, 'sourceLocationOverride': 'foreign-bucket/source.zip'}))
