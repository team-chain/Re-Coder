"""Refreshable credentials provided by the local extension, with no profile fallback."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

ENV_KEYS = (
    'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_CONTAINER_AUTHORIZATION_TOKEN',
    'AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
    'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CONFIG_FILE', 'AWS_EC2_METADATA_DISABLED',
    'RECODER_AWS_BROWSER_LOGIN', 'RECODER_AWS_ACCOUNT_ID', 'RECODER_ASSUME_ROLE_ARN',
    'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_ARN', 'AWS_ROLE_SESSION_NAME',
)


def active() -> bool:
    return os.environ.get('RECODER_AWS_BROWSER_LOGIN') == '1'


def configure(endpoint: str, token: str, region: str, account: str) -> None:
    # This is the extension's loopback credential bridge, not a configurable URL.
    url = urlsplit(endpoint)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
            or url.username or url.password or url.path != '/credentials' or url.query or url.fragment
            or not re.fullmatch(r'[A-Za-z0-9_-]{64}', token)
            or not re.fullmatch(r'\d{12}', account)
            or not re.fullmatch(r'(?:af|ap|ca|eu|il|me|mx|sa|us)-(?!gov|iso)[a-z]+-\d', region)):
        raise ValueError('브라우저 AWS 연결 정보가 올바르지 않습니다.')
    for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                'AWS_PROFILE', 'AWS_DEFAULT_PROFILE', *ENV_KEYS):
        os.environ.pop(key, None)
    os.environ.update({
        'AWS_CONTAINER_CREDENTIALS_FULL_URI': endpoint,
        'AWS_CONTAINER_AUTHORIZATION_TOKEN': token,
        'AWS_SHARED_CREDENTIALS_FILE': os.devnull, 'AWS_CONFIG_FILE': os.devnull,
        'AWS_EC2_METADATA_DISABLED': 'true', 'AWS_REGION': region, 'AWS_DEFAULT_REGION': region,
        'RECODER_AWS_BROWSER_LOGIN': '1', 'RECODER_AWS_ACCOUNT_ID': account,
    })


def leave() -> None:
    if not active():
        return
    for key in ENV_KEYS:
        os.environ.pop(key, None)


def validate_account(account: str) -> None:
    if active() and account != os.environ.get('RECODER_AWS_ACCOUNT_ID'):
        raise ValueError('연결한 AWS 계정과 실제 자격증명의 계정이 다릅니다. 다시 로그인하세요.')
