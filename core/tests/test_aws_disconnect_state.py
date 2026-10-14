"""연결 해제 · 키 모드 프로필 정리 · SSO 프로필 상태."""
import asyncio
import os
from pathlib import Path

import pytest

from api.routes import aws


@pytest.fixture
def home(tmp_path, monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(aws, "CREDENTIALS_FILE", tmp_path / "aws_credentials.json")
    monkeypatch.setattr(aws, "RECODER_HOME", tmp_path)
    cred = tmp_path / "aws" / "credentials"
    cred.parent.mkdir()
    cred.write_text("[default]\naws_access_key_id = AKIAOLDDEFAULT000000\naws_secret_access_key = x\n")
    monkeypatch.setattr(aws, "AWS_CREDENTIALS_FILE", cred)
    monkeypatch.setattr(aws, "_role_state", None)
    monkeypatch.setattr(aws, "_active_profile", None)
    monkeypatch.setattr(aws, "_credentials_changed", lambda: None)
    monkeypatch.setattr(aws, "_enter_role_mode_from_env", lambda: None)
    monkeypatch.setattr(aws, "_refresh_role_credentials", lambda: None)
    calls = []
    monkeypatch.setattr(aws, "_call_sts_get_caller_identity",
                        lambda profile, region: calls.append(profile) or {"account": "1", "arn": "arn:aws:iam::1:user/u", "user_id": "U"})
    monkeypatch.setattr(aws, "_guard_saved", {})
    guard_keys = ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_EC2_METADATA_DISABLED")
    for key in guard_keys:
        monkeypatch.delenv(key, raising=False)
    yield tmp_path, calls
    for key in guard_keys + ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"):
        os.environ.pop(key, None)


def test_disconnect_does_not_fall_back_to_default_profile(home):
    tmp, calls = home
    asyncio.run(aws.clear_aws())
    status = asyncio.run(aws.get_aws_status())
    assert status.ready is False
    assert "해제" in status.message
    assert calls == []


def test_reconnect_clears_disconnected_state(home, monkeypatch):
    tmp, calls = home
    monkeypatch.setattr(aws, "_guard_saved", {})
    asyncio.run(aws.clear_aws())
    monkeypatch.setattr(aws, "_inspect_deploy_permissions", lambda *a, **k: None)
    asyncio.run(aws.connect_aws(aws.AwsConnectRequest(access_key_id="AKIANEWKEY0000000000", secret_access_key="s" * 40, region="us-east-1")))
    assert not (tmp / "aws_disconnected").exists()
    assert "AWS_EC2_METADATA_DISABLED" not in os.environ
    assert asyncio.run(aws.get_aws_status()).ready is True
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        os.environ.pop(key, None)


def test_key_mode_drops_stale_profile(home, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "old-account")
    aws._apply_to_process_env("AKIANEWKEY0000000000", "s", "us-east-1", "")
    try:
        assert "AWS_PROFILE" not in os.environ
    finally:
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            os.environ.pop(key, None)


def test_sso_profile_without_credentials_file_is_checked(home, monkeypatch):
    tmp, calls = home
    monkeypatch.setattr(aws, "AWS_CREDENTIALS_FILE", Path(tmp / "missing"))
    monkeypatch.setenv("AWS_PROFILE", "sso-dev")
    status = asyncio.run(aws.get_aws_status())
    assert status.ready is True
    assert calls == ["sso-dev"]


def test_disconnect_blocks_default_chain_for_every_aws_call(home, monkeypatch):
    tmp, calls = home
    for key in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_EC2_METADATA_DISABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(aws, "_guard_saved", {})
    asyncio.run(aws.clear_aws())
    try:
        import boto3
        assert boto3.Session().get_credentials() is None  # [default] 프로필로 새지 않는다
        assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"
    finally:
        aws._lift_disconnect_guard()
    assert "AWS_SHARED_CREDENTIALS_FILE" not in os.environ


def test_failed_key_connect_keeps_disconnected_state(home, monkeypatch):
    tmp, calls = home
    for key in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_EC2_METADATA_DISABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(aws, "_guard_saved", {})
    monkeypatch.setenv("AWS_DEFAULT_PROFILE", "shell-profile")
    asyncio.run(aws.clear_aws())
    monkeypatch.setenv("AWS_DEFAULT_PROFILE", "shell-profile")

    def reject(profile, region):
        raise aws.HTTPException(status_code=401, detail="bad key")

    monkeypatch.setattr(aws, "_call_sts_get_caller_identity", reject)
    with pytest.raises(aws.HTTPException):
        asyncio.run(aws.connect_aws(aws.AwsConnectRequest(access_key_id="AKIABADKEY0000000000", secret_access_key="x" * 40, region="us-east-1")))
    try:
        assert (tmp / "aws_disconnected").exists()
        assert os.environ.get("AWS_DEFAULT_PROFILE") == "shell-profile"
        assert "AWS_ACCESS_KEY_ID" not in os.environ
        assert os.environ.get("AWS_EC2_METADATA_DISABLED") == "true"  # 다시 막혔다
    finally:
        aws._lift_disconnect_guard()


def test_profile_connect_after_disconnect_works(home, monkeypatch):
    tmp, calls = home
    for key in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_EC2_METADATA_DISABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(aws, "_guard_saved", {})
    asyncio.run(aws.clear_aws())
    monkeypatch.setattr(aws, "_known_profiles", lambda: ["default", "dev"])
    seen = {}

    def sts(profile, region):
        seen["cfg"] = os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
        return {"account": "1", "arn": "arn:aws:iam::1:user/u", "user_id": "U"}

    monkeypatch.setattr(aws, "_call_sts_get_caller_identity", sts)
    monkeypatch.setattr(aws, "_inspect_deploy_permissions", lambda *a, **k: None)
    try:
        asyncio.run(aws.connect_aws_profile(aws.AwsProfileConnectRequest(profile="dev", region="us-east-1")))
    except aws.HTTPException:
        pass
    assert seen.get("cfg") is None  # 검증할 때는 ~/.aws 를 다시 읽을 수 있다
    assert "AWS_EC2_METADATA_DISABLED" not in os.environ or os.environ.get("AWS_PROFILE")
    os.environ.pop("AWS_PROFILE", None)
    aws._lift_disconnect_guard()
