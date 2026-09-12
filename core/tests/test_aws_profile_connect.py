"""프로필 연결 — 키 재입력 없이 ~/.aws 프로필로 연결하는 경로.

여기서 막는 사고는 두 가지다.
1) 남아 있는 AWS_ACCESS_KEY_ID 가 AWS_PROFILE 을 이긴다. 지우지 않으면
   화면은 프로필 A 로 연결됐다고 표시되는데 실제 요청은 옛 키 B 로 나간다 —
   **다른 계정에 배포되는** 조용한 사고다.
2) credentials 파일만 읽으면 SSO 프로필(config 에만 존재)이 목록과 연결
   양쪽에서 빠져서, "aws cli 로는 되는데 여기엔 안 뜨는" 상태가 된다.
"""
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

_AWS_ENV_KEYS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION",
)


@pytest.fixture(autouse=True)
def _isolate_aws_environment(monkeypatch: pytest.MonkeyPatch):
    """성공한 연결은 설계상 환경을 되돌리지 않는다 — 그게 이 기능이다.

    대신 **테스트가** 반드시 되돌려야 한다. 안 되돌리면 여기서 세팅된
    AWS_PROFILE=dev 가 뒤에 도는 moto 기반 테스트로 새어 나가
    ProfileNotFound 로 무더기 실패한다(같은 파일 밖 15건).
    """
    before = {key: os.environ.get(key) for key in _AWS_ENV_KEYS}
    monkeypatch.setattr(aws, "_active_profile", aws._active_profile)
    yield
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _identity() -> dict[str, str]:
    return {
        "account": "123456789012",
        "arn": "arn:aws:iam::123456789012:user/dev",
        "user_id": "AIDA",
    }


def _write_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                    credentials: str = "", config: str = "") -> None:
    cred = tmp_path / "credentials"
    cfg = tmp_path / "config"
    if credentials:
        cred.write_text(credentials, encoding="utf-8")
    if config:
        cfg.write_text(config, encoding="utf-8")
    monkeypatch.setattr(aws, "AWS_CREDENTIALS_FILE", cred)
    monkeypatch.setattr(aws, "AWS_CONFIG_FILE", cfg)


def _quiet(monkeypatch: pytest.MonkeyPatch, sts=None) -> None:
    monkeypatch.setattr(
        aws, "_call_sts_get_caller_identity",
        sts or (lambda *, profile, region: _identity()),
    )
    monkeypatch.setattr(aws, "_inspect_deploy_permissions", lambda *a, **k: None)
    monkeypatch.setattr(aws, "_refresh_diagnostics_cache", lambda: None)


def test_route_is_registered() -> None:
    paths = {route.path for route in aws.router.routes}
    assert "/api/aws/connect-profile" in paths


def test_없는_프로필은_400_과_가능한_목록을_알려준다(tmp_path, monkeypatch) -> None:
    """boto3 에 그대로 넘기면 ProfileNotFound 가 500 으로 떨어진다."""
    _write_profiles(tmp_path, monkeypatch, credentials="[dev]\naws_access_key_id=x\n")
    _quiet(monkeypatch)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(aws.connect_aws_profile(
            aws.AwsProfileConnectRequest(profile="prod", region="us-east-1")
        ))
    assert exc.value.status_code == 400
    assert "dev" in str(exc.value.detail), "사용 가능한 프로필을 알려주지 않는다"


def test_프로필_연결은_키를_요청에_싣지_않는다(tmp_path, monkeypatch) -> None:
    _write_profiles(tmp_path, monkeypatch, credentials="[dev]\naws_access_key_id=x\n")
    _quiet(monkeypatch)

    status = asyncio.run(aws.connect_aws_profile(
        aws.AwsProfileConnectRequest(profile="dev", region="us-west-2")
    ))

    assert status.ready is True
    assert status.storage == "aws_profile"
    assert status.profile == "dev"
    assert status.region == "us-west-2"
    #: 키가 요청에 없으므로 마지막 4자리도 있을 수 없다. 여기 값이 있다면
    #: 어딘가에서 키를 읽어 왔다는 뜻이다.
    assert status.access_key_last4 == ""


def test_남아있던_키_환경변수를_지운다(tmp_path, monkeypatch) -> None:
    """[핵심] AWS_ACCESS_KEY_ID 는 AWS_PROFILE 보다 우선한다.

    이전 키 연결의 잔재를 지우지 않으면, 프로필 A 를 골랐는데 요청은
    옛 키 B 로 나간다 — 화면 표시와 실제 계정이 달라지는 조용한 사고.
    """
    _write_profiles(tmp_path, monkeypatch, credentials="[dev]\naws_access_key_id=x\n")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "OLD_KEY_FROM_BEFORE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "OLD_SECRET")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "OLD_TOKEN")

    seen: dict[str, object] = {}

    def sts(*, profile, region):
        #: STS 가 호출되는 시점(=boto3 가 자격증명을 해석하는 시점)에
        #: 키 환경변수가 이미 지워져 있어야 한다.
        seen["access_key_at_sts"] = os.environ.get("AWS_ACCESS_KEY_ID")
        seen["profile_at_sts"] = os.environ.get("AWS_PROFILE")
        return _identity()

    _quiet(monkeypatch, sts=sts)

    asyncio.run(aws.connect_aws_profile(
        aws.AwsProfileConnectRequest(profile="dev", region="us-east-1")
    ))

    assert seen["access_key_at_sts"] is None, "옛 키가 살아 있어 프로필을 이긴다"
    assert seen["profile_at_sts"] == "dev"


def test_STS_실패시_환경을_원상복구한다(tmp_path, monkeypatch) -> None:
    _write_profiles(tmp_path, monkeypatch, credentials="[dev]\naws_access_key_id=x\n")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ORIGINAL")
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    def sts(*, profile, region):
        raise RuntimeError("expired token")

    _quiet(monkeypatch, sts=sts)

    with pytest.raises(RuntimeError):
        asyncio.run(aws.connect_aws_profile(
            aws.AwsProfileConnectRequest(profile="dev", region="us-east-1")
        ))

    #: 실패한 연결 시도가 기존 연결 상태를 망가뜨리면 안 된다.
    assert os.environ.get("AWS_ACCESS_KEY_ID") == "ORIGINAL"
    assert os.environ.get("AWS_PROFILE") is None


def test_SSO_프로필도_목록과_연결에_포함된다(tmp_path, monkeypatch) -> None:
    """SSO 는 credentials 파일이 비어 있고 config 에만 [profile x] 가 있다."""
    _write_profiles(
        tmp_path, monkeypatch,
        config="[profile sso-dev]\nsso_start_url=https://x.awsapps.com/start\nregion=ap-northeast-2\n",
    )
    _quiet(monkeypatch)

    listed = asyncio.run(aws.list_aws_profiles())
    assert "sso-dev" in listed["profiles"], "SSO 프로필이 목록에서 빠진다"

    status = asyncio.run(aws.connect_aws_profile(
        aws.AwsProfileConnectRequest(profile="sso-dev", region="us-east-1")
    ))
    assert status.ready is True
    assert status.profile == "sso-dev"


def test_리전_미지정시_프로필_설정을_따른다(tmp_path, monkeypatch) -> None:
    """하드코딩 기본값으로 덮으면 키 연결 UI 가 겪은 리전 불일치 사고를 반복한다."""
    _write_profiles(tmp_path, monkeypatch, credentials="[dev]\naws_access_key_id=x\n")
    _quiet(monkeypatch)

    class _Session:
        region_name = "eu-west-1"

    monkeypatch.setattr(aws, "_build_boto3_session", lambda profile=None, region=None: _Session())

    status = asyncio.run(aws.connect_aws_profile(
        aws.AwsProfileConnectRequest(profile="dev", region="")
    ))
    assert status.region == "eu-west-1"


def test_음성대조_목록은_중복_프로필을_안_만든다(tmp_path, monkeypatch) -> None:
    _write_profiles(
        tmp_path, monkeypatch,
        credentials="[dev]\naws_access_key_id=x\n",
        config="[profile dev]\nregion=us-east-1\n",
    )
    listed = asyncio.run(aws.list_aws_profiles())
    assert listed["profiles"].count("dev") == 1
