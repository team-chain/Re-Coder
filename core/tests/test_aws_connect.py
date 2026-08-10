"""AWS connect는 STS 검증만 수행하며 키를 파일에 저장하지 않는지 확인한다."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from api.routes import aws  # noqa: E402


def test_connect_validates_and_keeps_credentials_in_current_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ORIGINAL_ACCESS_KEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ORIGINAL_SECRET")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    received: dict[str, str] = {}

    def fake_sts(*, profile: str | None, region: str) -> dict[str, str]:
        received["access_key"] = os.environ["AWS_ACCESS_KEY_ID"]
        received["secret_key"] = os.environ["AWS_SECRET_ACCESS_KEY"]
        received["region"] = region
        assert profile is None
        return {"account": "123456789012", "arn": "arn:aws:iam::123456789012:user/test", "user_id": "AIDA"}

    monkeypatch.setattr(aws, "_call_sts_get_caller_identity", fake_sts)
    monkeypatch.setattr(aws, "_refresh_diagnostics_cache", lambda: pytest.fail("connect must not refresh persistent diagnostics"))

    result = asyncio.run(aws.connect_aws(aws.AwsConnectRequest(
        access_key_id="AKIA12345678901234",
        secret_access_key="secret-key-for-test",
        region="ap-northeast-2",
    )))

    assert result.ready is True
    assert result.identity and result.identity.account == "123456789012"
    assert result.storage == "secret_storage"
    assert received == {
        "access_key": "AKIA12345678901234",
        "secret_key": "secret-key-for-test",
        "region": "ap-northeast-2",
    }
    # 키는 파일이 아니라 현재 Core 프로세스 메모리에만 적용된다. Extension은
    # 별도로 SecretStorage에 보관해 다음 Core 시작 시에만 다시 주입한다.
    assert os.environ["AWS_ACCESS_KEY_ID"] == "AKIA12345678901234"
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "secret-key-for-test"
    assert os.environ["AWS_REGION"] == "ap-northeast-2"


def test_connect_does_not_call_legacy_file_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aws, "_call_sts_get_caller_identity", lambda **_: {"account": "1", "arn": "arn", "user_id": "id"})
    monkeypatch.setattr(aws, "_save_recoder_credentials", lambda *args: pytest.fail("connect must not write ~/.recoder"))
    monkeypatch.setattr(aws, "_save_aws_credentials_file", lambda *args: pytest.fail("connect must not write ~/.aws"))

    result = asyncio.run(aws.connect_aws(aws.AwsConnectRequest(
        access_key_id="AKIA12345678901234",
        secret_access_key="secret-key-for-test",
    )))

    assert result.ready is True
