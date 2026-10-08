import boto3
from botocore.config import Config

import aws_client_defaults


def test_default_timeouts_applied_when_caller_gives_no_config():
    assert aws_client_defaults.install()
    client = boto3.session.Session(aws_access_key_id="AKIAxxxxxxxxxxxxxxxx", aws_secret_access_key="x", region_name="us-east-1").client("sts")
    cfg = client.meta.config
    assert cfg.connect_timeout == 5
    assert cfg.read_timeout == 60



def test_user_retry_settings_are_not_overridden(monkeypatch):
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "7")
    aws_client_defaults.install()
    client = boto3.session.Session(aws_access_key_id="AKIAxxxxxxxxxxxxxxxx", aws_secret_access_key="x", region_name="us-east-1").client("sts")
    assert client.meta.config.retries.get("total_max_attempts") == 7


def test_caller_config_wins():
    aws_client_defaults.install()
    aws_client_defaults.install()  # idempotent
    client = boto3.session.Session(aws_access_key_id="AKIAxxxxxxxxxxxxxxxx", aws_secret_access_key="x", region_name="us-east-1").client(
        "ecs", config=Config(read_timeout=5, retries={"max_attempts": 0}))
    cfg = client.meta.config
    assert cfg.read_timeout == 5
    assert cfg.connect_timeout == 5
    assert cfg.retries.get("mode") != "standard"  # 호출자가 준 재시도 설정이 그대로 쓰인다
