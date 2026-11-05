"""Restricted runner transport using the existing Core AWS session; never exports keys."""
from __future__ import annotations

import base64
import json
import re
from typing import Literal
from urllib.parse import parse_qsl, quote

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from botocore.auth import SigV4Auth, S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import URLLib3Session

KINDS = ('security', 'build', 'container')
PROJECTS = {f'recoder-{kind}-v1' for kind in KINDS}
BUILD_ID = re.compile(r'recoder-(?:security|build|container)-v1:[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z')


class ManagedAwsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', regex_engine='python-re')
    account: str = Field(pattern=r'^\d{12}$')
    region: str = Field(pattern=r'^(?:af|ap|ca|eu|il|me|mx|sa|us)-(?!gov|iso)[a-z]+-\d$')
    connection_id: str = Field(min_length=32, max_length=64)
    service: Literal['cloudformation', 'codebuild', 'dynamodb', 's3']
    method: Literal['POST', 'GET', 'PUT', 'DELETE'] = 'POST'
    body_base64: str = Field(default='', max_length=48 * 1024 * 1024)
    bucket: str = Field(default='', max_length=120)
    key: str = Field(default='', max_length=1024)
    target: str = Field(default='', max_length=100)
    content_type: str = Field(default='application/octet-stream', max_length=100)
    limit: Literal[4194304, 5242880] = 4194304


def _require(condition, message='관리형 실행 요청 범위를 벗어났습니다.'):
    if not condition:
        raise HTTPException(400, detail=message)


def validate_request(req: ManagedAwsRequest) -> bytes:
    try:
        body = base64.b64decode(req.body_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, detail='관리형 요청 본문 형식이 잘못되었습니다.') from exc
    _require(len(body) <= 36 * 1024 * 1024)
    if req.service == 's3':
        _require(req.bucket in {f'recoder-{k}-{req.account}-{req.region}' for k in KINDS})
        _require(not req.target and req.method in ('GET', 'PUT', 'DELETE'))
        pattern = (r'reports/rc-rt-[a-f0-9]{24}/(?:report\.json|progress\.json|sbom\.json|files/\d+)'
                   if req.method == 'GET' else r'sources/rc-rt-[a-f0-9]{24}\.zip')
        _require(bool(re.fullmatch(pattern, req.key)))
        _require(req.method == 'PUT' or not body)
        return body
    _require(req.method == 'POST' and not req.bucket and not req.key and len(body) <= 128 * 1024)
    try:
        if req.service == 'cloudformation':
            _require(not req.target and req.content_type == 'application/x-www-form-urlencoded')
            pairs = parse_qsl(body.decode(), strict_parsing=True)
            values = dict(pairs)
            _require(len(pairs) == len(values) and values.get('Version') == '2010-05-15')
            _require(values.get('Action') in ('DescribeStacks', 'CreateStack') and values.get('StackName') in PROJECTS)
            allowed = {'Action', 'Version', 'StackName'}
            if values['Action'] == 'CreateStack':
                allowed |= {'TemplateBody', 'Capabilities.member.1', 'Tags.member.1.Key', 'Tags.member.1.Value', 'Tags.member.2.Key', 'Tags.member.2.Value', 'OnFailure', 'ClientRequestToken'}
                _require('TemplateBody' in values and len(values['TemplateBody'].encode()) <= 51200 and values.get('Capabilities.member.1') == 'CAPABILITY_IAM')
            _require(set(values) <= allowed)
        else:
            values = json.loads(body)
            _require(isinstance(values, dict))
            if req.service == 'codebuild':
                prefix = 'CodeBuild_20161006.'
                _require(req.target.startswith(prefix) and req.content_type == 'application/x-amz-json-1.1')
                action = req.target[len(prefix):]
                _require(action in ('StartBuild', 'ListBuildsForProject', 'BatchGetBuilds', 'StopBuild'))
                if action in ('StartBuild', 'ListBuildsForProject'):
                    _require(values.get('projectName') in PROJECTS)
                elif action == 'StopBuild':
                    _require(isinstance(values.get('id'), str) and BUILD_ID.fullmatch(values['id']))
                else:
                    ids = values.get('ids')
                    _require(isinstance(ids, list) and 0 < len(ids) <= 100 and all(isinstance(x, str) and BUILD_ID.fullmatch(x) for x in ids))
                if action == 'StartBuild':
                    _require(set(values) <= {'projectName', 'idempotencyToken', 'sourceLocationOverride', 'sourceTypeOverride', 'environmentVariablesOverride', 'artifactsOverride'})
                    run = values.get('idempotencyToken', '')
                    _require(isinstance(run, str) and re.fullmatch(r'rc-rt-[a-f0-9]{24}', run))
                    kind = values['projectName'].removeprefix('recoder-').removesuffix('-v1')
                    bucket = f'recoder-{kind}-{req.account}-{req.region}'
                    _require(values.get('sourceTypeOverride') == 'S3' and values.get('sourceLocationOverride') == f'{bucket}/sources/{run}.zip')
                    artifacts = values.get('artifactsOverride', {})
                    _require(artifacts == {'type': 'S3', 'location': bucket, 'path': 'reports', 'namespaceType': 'NONE', 'name': run, 'packaging': 'NONE', 'encryptionDisabled': False})
                    env = values.get('environmentVariablesOverride', [])
                    _require(isinstance(env, list) and len(env) == 3 and all(isinstance(e, dict) and set(e) == {'name', 'value', 'type'} and e['type'] == 'PLAINTEXT' for e in env))
                    data = {e['name']: e['value'] for e in env}
                    _require(set(data) == {'RECODER_RUN_ID', 'RECODER_SOURCE_SHA256', 'RECODER_REPORT_BUCKET'} and data['RECODER_RUN_ID'] == run and data['RECODER_REPORT_BUCKET'] == bucket and isinstance(data['RECODER_SOURCE_SHA256'], str) and re.fullmatch(r'[a-f0-9]{64}', data['RECODER_SOURCE_SHA256']))
            else:
                _require(req.target in {f'DynamoDB_20120810.{a}' for a in ('GetItem', 'PutItem', 'UpdateItem', 'DeleteItem')} and req.content_type == 'application/x-amz-json-1.0')
                _require(values.get('TableName') in {f'recoder-{k}-lock-v1' for k in KINDS})
                key = values.get('Item') if req.target.endswith('.PutItem') else values.get('Key')
                _require(isinstance(key, dict) and key.get('pk') == {'S': 'active'})
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise HTTPException(400, detail='관리형 AWS 요청 형식이 잘못되었습니다.') from exc
    return body


def execute(req: ManagedAwsRequest, body: bytes, credentials, transport=None) -> dict:
    """No arbitrary URLs, redirects, retries or credential response fields."""
    host = f'{req.bucket}.s3.{req.region}.amazonaws.com' if req.service == 's3' else f'{req.service}.{req.region}.amazonaws.com'
    url = f'https://{host}/' + quote(req.key, safe='/')
    headers = {'Content-Type': req.content_type}
    if req.target:
        headers['X-Amz-Target'] = req.target
    request = AWSRequest(method=req.method, url=url, data=body, headers=headers, stream_output=True)
    signer = S3SigV4Auth if req.service == 's3' else SigV4Auth
    signer(credentials, req.service, req.region).add_auth(request)
    http = transport or URLLib3Session(timeout=60)
    response = None
    try:
        response = http.send(request.prepare())
        data = response.raw.read(req.limit + 1)
        _require(len(data) <= req.limit, 'AWS 응답 크기 제한을 초과했습니다.')
        if not 200 <= response.status_code < 300:
            code = 'RequestFailed'
            match = re.search(rb'<Code>([A-Za-z0-9]+)</Code>', data)
            if match:
                code = match[1].decode()
            try:
                code = str(json.loads(data).get('__type', code)).split('#')[-1]
            except (ValueError, AttributeError):
                pass
            return {'status': response.status_code, 'error_code': re.sub('[^A-Za-z0-9]', '', code)[:80]}
        return {'status': response.status_code, 'body_base64': base64.b64encode(data).decode()}
    except HTTPException:
        raise
    except Exception as exc:
        # A transport failure may occur AFTER StartBuild was accepted. Never retry here.
        raise HTTPException(502, detail='AWS 응답을 확인하지 못했습니다. 새 실행 전에 상태 확인으로 복구하세요.') from exc
    finally:
        if response is not None:
            response.raw.close()
        if transport is None:
            http.close()
