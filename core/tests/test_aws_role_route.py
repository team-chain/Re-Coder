"""/api/aws/role/setup · refresh — 역할 모드가 코어 프로세스에 어떻게 얹히나.

여기서 막는 사고
    1. 역할을 빌린 뒤에도 AWS_PROFILE 이 남아 있으면 사람은 프로필로
       나간다고 믿는데 실제 요청은 임시 자격증명으로 나간다(또는 반대).
       역할 모드에선 프로필 환경변수를 지우고, status 는 "assumed_role" 이다.
    2. 권한이 모자라면 500 이 아니라 **200 + console_fallback** 이다. 화면은
       이 값을 보고 콘솔 경로를 연다. 그리고 환경은 원래대로다 — 실패한
       셋업이 기반 자격증명을 망가뜨리면 안 된다.
    3. status 는 역할 모드에서 기반(프로필)이 아니라 빌린 자격증명을 검증한다.
    4. 키/프로필로 새로 연결하면 역할 모드가 풀린다 — 기반이 바뀌었는데 옛
       역할 상태가 남아 "역할로 연결됨"이라 표시되면 안 된다.
    5. 코어가 RECODER_ASSUME_ROLE_ARN 을 받고 뜨면 첫 status 에서 역할을
       빌린다 — 확장은 재시작마다 역할 ARN(비밀 아님)만 넘긴다.
    6. 만료 임박이면 status 조회가 갱신한다.

boto3 는 부르지 않는다 — aws_role 의 함수를 바꿔 끼운다. 그쪽 동작은
test_aws_role.py 가 가짜 IAM/STS 로 따로 고정한다.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import aws_role  # noqa: E402
from api.routes import aws  # noqa: E402

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{aws_role.ROLE_NAME}"
_AWS_ENV_KEYS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", aws.ENV_ASSUME_ROLE_ARN,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    before = {key: os.environ.get(key) for key in _AWS_ENV_KEYS}
    for key in _AWS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(aws, "_active_profile", None)
    monkeypatch.setattr(aws, "_role_state", None)
    monkeypatch.setattr(aws, "_refresh_diagnostics_cache", lambda: None)
    monkeypatch.setattr(aws, "_inspect_deploy_permissions", lambda *a, **k: None)
    #: 파일 기반 폴백 경로가 개발자 홈을 읽지 않게.
    monkeypatch.setattr(aws, "CREDENTIALS_FILE", Path("/nonexistent/aws_credentials.json"))
    #: **개발자의 실제 ~/.aws 도 읽으면 안 된다.**
    #:
    #: `setup_aws_role` 은 환경변수도 프로필도 없으면 `_detect_credential_source()`
    #: 로 한 번 더 기반을 찾는데, 그게 `AWS_CREDENTIALS_FILE`(기본값 ~/.aws/
    #: credentials)을 본다. AWS 를 쓰는 사람 컴퓨터에는 그 파일이 있으니
    #: `default` 프로필이 잡혀 "기반 없음 → 400" 이 안 났다. 리눅스 CI 에는
    #: 그 파일이 없어 통과하고 팀원 PC 에서만 깨지는, 환경 의존 테스트였다
    #: (2026-09-22 실기기 Windows). 프로필이 필요한 테스트는 `_profiles()` 가
    #: 이 값을 tmp_path 로 다시 덮는다.
    monkeypatch.setattr(aws, "AWS_CREDENTIALS_FILE", Path("/nonexistent/.aws/credentials"))
    monkeypatch.setattr(aws, "AWS_CONFIG_FILE", Path("/nonexistent/.aws/config"))
    yield
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names=("dev",)) -> None:
    cred = tmp_path / "credentials"
    cred.write_text("".join(f"[{n}]\naws_access_key_id = AKIA{n.upper()}0000000000\n"
                            f"aws_secret_access_key = s\n" for n in names), encoding="utf-8")
    monkeypatch.setattr(aws, "AWS_CREDENTIALS_FILE", cred)
    monkeypatch.setattr(aws, "AWS_CONFIG_FILE", tmp_path / "config")


class _FakeSession:
    """boto3.Session 대역 — 어떤 기반으로 만들어졌는지 기록만 한다."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.region_name = kwargs.get("region_name") or REGION

    def client(self, name: str, region_name=None):
        if name == "sts":
            return SimpleNamespace(get_caller_identity=lambda: {
                "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/dev", "UserId": "AIDA",
            })
        return SimpleNamespace(name=name)


def _creds(minutes: int = 60) -> aws_role.TemporaryCredentials:
    return aws_role.TemporaryCredentials(
        "ASIAROLE", "secret", "token",
        datetime.now(timezone.utc) + timedelta(minutes=minutes),
    )


def _wire(monkeypatch: pytest.MonkeyPatch, *, denied: str = "", created: bool = True):
    """aws_role 의 IAM/STS 를 만지는 두 함수를 바꿔 끼운다. 호출 기록을 돌려준다."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(aws, "_base_session", lambda p, e, r: _FakeSession(profile=p, env=e, region_name=r))
    monkeypatch.setattr(aws, "_build_boto3_session", lambda profile=None, region=None: _FakeSession(profile_name=profile, region_name=region))

    def ensure(iam, **kw):
        calls["ensure"] = kw
        if denied and denied != "sts:AssumeRole":
            raise aws_role.RoleSetupDenied(denied)
        return aws_role.RoleSetupResult(
            role_arn=ROLE_ARN, role_name=aws_role.ROLE_NAME, created=created,
            trust_updated=False, policy_statements=27, principal_arn=kw["principal_arn"],
        )

    def assume(sts, role_arn, **kw):
        calls["assume"] = role_arn
        calls["assume_count"] = int(calls.get("assume_count", 0)) + 1
        if denied == "sts:AssumeRole":
            raise aws_role.RoleSetupDenied("sts:AssumeRole")
        return _creds()

    monkeypatch.setattr(aws_role, "ensure_deploy_role", ensure)
    monkeypatch.setattr(aws_role, "assume_deploy_role", assume)
    #: 빌린 자격증명 검증 — 환경변수의 키가 임시 키여야 한다.
    monkeypatch.setattr(aws, "_call_sts_get_caller_identity", lambda *, profile, region: {
        "account": ACCOUNT,
        "arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/{aws_role.ROLE_NAME}/recoder",
        "user_id": "AROA:recoder",
    })
    return calls


def _setup(profile: str = "dev", region: str = "") -> aws.AwsRoleSetupResponse:
    return asyncio.run(aws.setup_aws_role(aws.AwsRoleSetupRequest(profile=profile, region=region)))


def test_routes_are_registered() -> None:
    paths = {route.path for route in aws.router.routes}
    assert {"/api/aws/role/setup", "/api/aws/role/refresh"} <= paths


def test_프로필로_역할을_만들고_빌리면_코어는_임시_자격증명만_쓴다(tmp_path, monkeypatch) -> None:
    _profiles(tmp_path, monkeypatch)
    calls = _wire(monkeypatch)
    resp = _setup("dev")
    assert resp.ok is True and resp.mode == "role"
    assert calls["assume"] == ROLE_ARN
    assert calls["ensure"]["account"] == ACCOUNT
    assert calls["ensure"]["principal_arn"] == f"arn:aws:iam::{ACCOUNT}:user/dev"
    #: 환경: 임시 키 + 토큰, 프로필은 지움 (사고 1)
    assert os.environ["AWS_ACCESS_KEY_ID"] == "ASIAROLE"
    assert os.environ["AWS_SESSION_TOKEN"] == "token"
    assert "AWS_PROFILE" not in os.environ
    assert os.environ[aws.ENV_ASSUME_ROLE_ARN] == ROLE_ARN
    assert resp.status is not None
    assert resp.status.storage == "assumed_role"
    assert resp.status.role_arn == ROLE_ARN and resp.status.expires_at
    assert resp.status.profile == "dev"        # 기반이 무엇이었는지는 보여 준다
    assert resp.role and "secret" not in str(resp.role) and "token" not in str(resp.role)


def test_모르는_프로필은_400(tmp_path, monkeypatch) -> None:
    _profiles(tmp_path, monkeypatch)
    _wire(monkeypatch)
    with pytest.raises(aws.HTTPException) as info:
        _setup("nope")
    assert info.value.status_code == 400


def test_기반_자격증명이_없으면_400(monkeypatch) -> None:
    _wire(monkeypatch)
    with pytest.raises(aws.HTTPException) as info:
        _setup("")
    assert info.value.status_code == 400


def test_지금_연결된_키가_기반이_될_수_있다(monkeypatch) -> None:
    calls = _wire(monkeypatch)
    os.environ["AWS_ACCESS_KEY_ID"] = "AKIABASE"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "basesecret"
    resp = _setup("")
    assert resp.ok is True
    #: 갱신용 기반은 프로세스 메모리에만 남는다 — 응답·환경에는 안 나간다.
    assert aws._role_state["base_env"]["AWS_ACCESS_KEY_ID"] == "AKIABASE"
    assert aws._role_state["base_profile"] == ""
    assert os.environ["AWS_ACCESS_KEY_ID"] == "ASIAROLE"
    assert "AKIABASE" not in str(resp.model_dump())


@pytest.mark.parametrize("denied", ["iam:CreateRole", "iam:PutRolePolicy", "sts:AssumeRole"])
def test_권한이_모자라면_콘솔_폴백_200_이고_환경은_그대로(tmp_path, monkeypatch, denied) -> None:
    _profiles(tmp_path, monkeypatch)
    _wire(monkeypatch, denied=denied)
    os.environ["AWS_PROFILE"] = "dev"
    resp = _setup("dev")
    assert resp.ok is False and resp.mode == "console_fallback"
    assert resp.denied_action == denied
    assert denied in resp.message
    assert os.environ.get("AWS_PROFILE") == "dev"         # 사고 2
    assert "AWS_ACCESS_KEY_ID" not in os.environ
    assert aws._role_state is None
    assert aws.ENV_ASSUME_ROLE_ARN not in os.environ


def test_status_는_역할_모드에서_기반이_아니라_빌린_자격증명을_본다(tmp_path, monkeypatch) -> None:
    _profiles(tmp_path, monkeypatch)
    _wire(monkeypatch)
    _setup("dev")
    seen: dict[str, object] = {}

    def sts(*, profile, region):
        seen["profile"] = profile
        return {"account": ACCOUNT, "arn": "x", "user_id": "y"}

    monkeypatch.setattr(aws, "_call_sts_get_caller_identity", sts)
    status = asyncio.run(aws.get_aws_status())
    assert seen["profile"] is None                       # 사고 3
    assert status.ready is True and status.storage == "assumed_role"
    assert status.role_arn == ROLE_ARN
    assert "역할" in status.message


def test_프로필로_다시_연결하면_역할_모드가_풀린다(tmp_path, monkeypatch) -> None:
    _profiles(tmp_path, monkeypatch, names=("dev", "prod"))
    _wire(monkeypatch)
    _setup("dev")
    assert aws._role_state is not None
    monkeypatch.setattr(aws, "_call_sts_get_caller_identity",
                        lambda *, profile, region: {"account": ACCOUNT, "arn": "x", "user_id": "y"})
    asyncio.run(aws.connect_aws_profile(aws.AwsProfileConnectRequest(profile="prod", region=REGION)))
    assert aws._role_state is None                        # 사고 4
    assert os.environ.get("AWS_PROFILE") == "prod"
    assert "AWS_SESSION_TOKEN" not in os.environ


def test_clear_는_역할_모드도_끝낸다(tmp_path, monkeypatch) -> None:
    _profiles(tmp_path, monkeypatch)
    _wire(monkeypatch)
    _setup("dev")
    asyncio.run(aws.clear_aws())
    assert aws._role_state is None
    assert aws.ENV_ASSUME_ROLE_ARN not in os.environ
    assert "AWS_SESSION_TOKEN" not in os.environ


def test_코어_시작_시_역할_ARN_환경변수가_있으면_첫_status_에서_빌린다(monkeypatch) -> None:
    calls = _wire(monkeypatch)
    os.environ["AWS_PROFILE"] = "dev"
    os.environ["AWS_REGION"] = REGION
    os.environ[aws.ENV_ASSUME_ROLE_ARN] = ROLE_ARN
    monkeypatch.setattr(aws, "AWS_CREDENTIALS_FILE", Path("/nonexistent/credentials"))
    status = asyncio.run(aws.get_aws_status())
    assert calls["assume"] == ROLE_ARN                    # 사고 5
    assert status.storage == "assumed_role"
    assert os.environ["AWS_ACCESS_KEY_ID"] == "ASIAROLE"
    assert "AWS_PROFILE" not in os.environ
    assert aws._role_state["base_profile"] == "dev"


def test_시작_시_빌리기가_실패해도_기반_자격증명으로_계속간다(monkeypatch) -> None:
    _wire(monkeypatch, denied="sts:AssumeRole")
    os.environ["AWS_PROFILE"] = "dev"
    os.environ[aws.ENV_ASSUME_ROLE_ARN] = ROLE_ARN
    monkeypatch.setattr(aws, "AWS_CREDENTIALS_FILE", Path("/nonexistent/credentials"))
    monkeypatch.setattr(aws, "_call_sts_get_caller_identity",
                        lambda *, profile, region: {"account": ACCOUNT, "arn": "x", "user_id": "y"})
    status = asyncio.run(aws.get_aws_status())
    assert aws._role_state is None
    assert os.environ.get("AWS_PROFILE") == "dev"
    assert status.storage != "assumed_role"


def test_만료_임박이면_status_가_갱신하고_refresh_는_즉시_갱신한다(tmp_path, monkeypatch) -> None:
    _profiles(tmp_path, monkeypatch)
    calls = _wire(monkeypatch)
    _setup("dev")
    assert calls["assume_count"] == 1
    #: 아직 여유 — 갱신 안 함
    asyncio.run(aws.get_aws_status())
    assert calls["assume_count"] == 1
    #: 만료 5분 전 — 갱신
    aws._role_state["expires_at"] = datetime.now(timezone.utc) + timedelta(minutes=5)
    asyncio.run(aws.get_aws_status())
    assert calls["assume_count"] == 2                     # 사고 6
    #: 강제 갱신
    status = asyncio.run(aws.refresh_aws_role())
    assert calls["assume_count"] == 3 and status.storage == "assumed_role"


def test_역할_모드가_아니면_refresh_는_400(monkeypatch) -> None:
    with pytest.raises(aws.HTTPException) as info:
        asyncio.run(aws.refresh_aws_role())
    assert info.value.status_code == 400


def test_명시_연결_없이_기본_프로필만_있어도_역할_전환이_된다(tmp_path, monkeypatch) -> None:
    """실기기(2026-09-21): ~/.aws 기본 프로필로 '연결됨' 인데 '배포 전용 역할로 전환' 이 400.
    status 가 기본 프로필을 연결로 치면 역할 셋업도 같은 기반을 써야 한다."""
    _profiles(tmp_path, monkeypatch, names=("default",))
    calls = _wire(monkeypatch)
    resp = _setup("")            # profile 비움 + env 키 없음 + AWS_PROFILE 없음
    assert resp.ok is True
    assert aws._role_state["base_profile"] == "default"
    assert calls["assume"] == ROLE_ARN
