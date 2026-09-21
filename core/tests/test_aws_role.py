"""프로그램 안 최소권한 역할 — 보드 카드 「AWS 온보딩 마찰 제거」 두 번째 절반.

여기서 고정하는 것
    1. 신뢰 정책은 **호출한 주체만** 넣는다. assumed-role(SSO) 은 밑의 IAM
       역할로 바꾼다 — 세션 ARN 을 그대로 넣으면 다음 로그인부터 못 빌린다.
    2. ensure_deploy_role 은 멱등이다. 두 번 불러도 CreateRole 은 한 번,
       정책은 매번 덮어쓴다(원본 aws_policy 가 바뀌면 따라오게).
    3. 이미 있는 역할에 다른 주체가 붙어 있으면 **지우지 않고 추가**한다.
       팀원 둘이 같은 계정에 온보딩하면 둘 다 빌릴 수 있어야 한다.
    4. 권한 정책은 aws_policy.build_policy() 와 한 벌 + 자기 역할 읽기 한 줄.
       자리표시자가 남으면 안 된다(계정·리전을 실제로 채운다).
    5. 권한 부족은 RoleSetupDenied(어느 액션인지) 다 — 호출자가 콘솔 폴백을
       고르는 근거라 예외 타입과 action 이 계약이다.
    6. CreateRole 직후 AssumeRole 의 전파 지연 거부는 재시도로 넘긴다.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import aws_policy  # noqa: E402
import aws_role  # noqa: E402

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
USER_ARN = f"arn:aws:iam::{ACCOUNT}:user/dev"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{aws_role.ROLE_NAME}"


class _ClientError(Exception):
    """botocore.exceptions.ClientError 와 같은 모양 — response["Error"]["Code"]."""

    def __init__(self, code: str, message: str = "") -> None:
        self.response = {"Error": {"Code": code, "Message": message or code}}
        super().__init__(f"An error occurred ({code}): {message or code}")


class FakeIam:
    """IAM 의 우리가 쓰는 네 호출만 흉내 낸다. 호출 기록을 남겨 멱등성을 잰다."""

    def __init__(self, existing_trust: dict | None = None, deny: set[str] | None = None) -> None:
        self.roles: dict[str, dict] = {}
        if existing_trust is not None:
            self.roles[aws_role.ROLE_NAME] = {
                "Arn": ROLE_ARN, "AssumeRolePolicyDocument": existing_trust,
            }
        self.inline: dict[tuple[str, str], dict] = {}
        self.deny = deny or set()
        self.calls: list[str] = []

    def _check(self, action: str) -> None:
        if action in self.deny:
            raise _ClientError("AccessDenied", f"not authorized to perform: {action}")

    def get_role(self, RoleName: str) -> dict:
        self.calls.append("GetRole")
        self._check("iam:GetRole")
        if RoleName not in self.roles:
            raise _ClientError("NoSuchEntity", f"role {RoleName} not found")
        return {"Role": dict(self.roles[RoleName], RoleName=RoleName)}

    def create_role(self, RoleName: str, AssumeRolePolicyDocument: str, **kw) -> dict:
        self.calls.append("CreateRole")
        self._check("iam:CreateRole")
        self.roles[RoleName] = {
            "Arn": f"arn:aws:iam::{ACCOUNT}:role/{RoleName}",
            "AssumeRolePolicyDocument": json.loads(AssumeRolePolicyDocument),
        }
        return {"Role": dict(self.roles[RoleName], RoleName=RoleName)}

    def update_assume_role_policy(self, RoleName: str, PolicyDocument: str) -> None:
        self.calls.append("UpdateAssumeRolePolicy")
        self._check("iam:UpdateAssumeRolePolicy")
        self.roles[RoleName]["AssumeRolePolicyDocument"] = json.loads(PolicyDocument)

    def put_role_policy(self, RoleName: str, PolicyName: str, PolicyDocument: str) -> None:
        self.calls.append("PutRolePolicy")
        self._check("iam:PutRolePolicy")
        self.inline[(RoleName, PolicyName)] = json.loads(PolicyDocument)


class FakeSts:
    def __init__(self, deny_first: int = 0, deny_always: bool = False) -> None:
        self.deny_first = deny_first
        self.deny_always = deny_always
        self.calls = 0

    def assume_role(self, RoleArn: str, RoleSessionName: str, DurationSeconds: int) -> dict:
        self.calls += 1
        if self.deny_always or self.calls <= self.deny_first:
            raise _ClientError("AccessDenied", "not authorized to perform: sts:AssumeRole")
        return {"Credentials": {
            "AccessKeyId": "ASIAFAKE",
            "SecretAccessKey": "secret",
            "SessionToken": "token",
            "Expiration": datetime.now(timezone.utc) + timedelta(seconds=DurationSeconds),
        }}


def _ensure(iam: FakeIam, principal: str = USER_ARN) -> aws_role.RoleSetupResult:
    return aws_role.ensure_deploy_role(
        iam, account=ACCOUNT, region=REGION, principal_arn=principal,
    )


# ---------------------------------------------------------------------------
# 1. 신뢰 정책 주체
# ---------------------------------------------------------------------------


def test_iam_사용자와_역할_ARN_은_그대로_주체가_된다() -> None:
    assert aws_role.principal_for_trust(USER_ARN, ACCOUNT) == USER_ARN
    role = f"arn:aws:iam::{ACCOUNT}:role/Admin"
    assert aws_role.principal_for_trust(role, ACCOUNT) == role


def test_assumed_role_세션은_밑의_IAM_역할로_바꾼다() -> None:
    sts_arn = f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_Admin_abc/sebin"
    assert aws_role.principal_for_trust(sts_arn, ACCOUNT) == (
        f"arn:aws:iam::{ACCOUNT}:role/AWSReservedSSO_Admin_abc"
    )


def test_root_와_알_수_없는_ARN_은_계정_root() -> None:
    root = f"arn:aws:iam::{ACCOUNT}:root"
    assert aws_role.principal_for_trust(root, ACCOUNT) == root
    assert aws_role.principal_for_trust("", ACCOUNT) == root
    with pytest.raises(ValueError):
        aws_role.principal_for_trust("", "")


def test_신뢰_정책은_주체만_assume_할_수_있고_중복은_없다() -> None:
    trust = aws_role.build_trust_policy([USER_ARN, USER_ARN])
    st = trust["Statement"][0]
    assert st["Action"] == "sts:AssumeRole"
    assert st["Principal"] == {"AWS": USER_ARN}
    two = aws_role.build_trust_policy([USER_ARN, f"arn:aws:iam::{ACCOUNT}:user/other"])
    assert len(two["Statement"][0]["Principal"]["AWS"]) == 2
    assert aws_role.principals_in(two) == [USER_ARN, f"arn:aws:iam::{ACCOUNT}:user/other"]


# ---------------------------------------------------------------------------
# 2·3. 멱등 · 주체 추가
# ---------------------------------------------------------------------------


def test_없으면_만들고_다시_부르면_만들지_않는다() -> None:
    iam = FakeIam()
    first = _ensure(iam)
    assert first.created is True and first.role_arn == ROLE_ARN
    assert iam.calls == ["GetRole", "CreateRole", "PutRolePolicy"]

    second = _ensure(iam)
    assert second.created is False and second.trust_updated is False
    assert iam.calls.count("CreateRole") == 1
    #: 정책은 매번 덮어쓴다 — 원본(aws_policy)이 바뀌면 다음 셋업에서 따라온다.
    assert iam.calls.count("PutRolePolicy") == 2


def test_다른_주체가_이미_있으면_지우지_않고_추가한다() -> None:
    other = f"arn:aws:iam::{ACCOUNT}:user/teammate"
    iam = FakeIam(existing_trust=aws_role.build_trust_policy([other]))
    result = _ensure(iam)
    assert result.created is False and result.trust_updated is True
    trust = iam.roles[aws_role.ROLE_NAME]["AssumeRolePolicyDocument"]
    assert set(aws_role.principals_in(trust)) == {other, USER_ARN}


def test_이미_신뢰된_주체면_신뢰_정책을_건드리지_않는다() -> None:
    iam = FakeIam(existing_trust=aws_role.build_trust_policy([USER_ARN]))
    result = _ensure(iam)
    assert result.trust_updated is False
    assert "UpdateAssumeRolePolicy" not in iam.calls


# ---------------------------------------------------------------------------
# 4. 권한 정책 = aws_policy 한 벌 + 자기 읽기
# ---------------------------------------------------------------------------


def test_역할_정책은_aws_policy_와_한_벌이고_자기_읽기_한_줄만_더_있다() -> None:
    iam = FakeIam()
    _ensure(iam)
    attached = iam.inline[(aws_role.ROLE_NAME, aws_role.ROLE_POLICY_NAME)]
    exec_role, task_role = aws_policy.resolve_roles("", "")
    expected = aws_policy.build_policy(
        None, account_id=ACCOUNT, region=REGION,
        task_execution_role=exec_role, task_role=task_role,
    )
    assert attached["Statement"][:-1] == expected["Statement"]
    own = attached["Statement"][-1]
    assert own["Sid"] == "ReadOwnRolePolicies"
    assert own["Resource"] == ROLE_ARN          # role/* 가 아니라 자기 자신만
    assert not aws_policy.has_placeholder(attached)
    assert aws_role.policy_fits_inline(attached)


def test_계정_리전이_비면_정책을_만들지_않는다() -> None:
    with pytest.raises(ValueError):
        aws_role.build_role_policy("", "", ROLE_ARN)


# ---------------------------------------------------------------------------
# 5. 권한 부족 → RoleSetupDenied(action)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("denied", ["iam:GetRole", "iam:CreateRole", "iam:PutRolePolicy"])
def test_권한이_없으면_어느_액션인지_알려_준다(denied: str) -> None:
    iam = FakeIam(deny={denied})
    with pytest.raises(aws_role.RoleSetupDenied) as info:
        _ensure(iam)
    assert info.value.action == denied


def test_신뢰_정책_갱신_권한이_없어도_어느_액션인지_알려_준다() -> None:
    other = f"arn:aws:iam::{ACCOUNT}:user/teammate"
    iam = FakeIam(existing_trust=aws_role.build_trust_policy([other]),
                  deny={"iam:UpdateAssumeRolePolicy"})
    with pytest.raises(aws_role.RoleSetupDenied) as info:
        _ensure(iam)
    assert info.value.action == "iam:UpdateAssumeRolePolicy"


def test_권한_외_오류는_그대로_올라온다() -> None:
    class Broken(FakeIam):
        def get_role(self, RoleName: str) -> dict:
            raise _ClientError("Throttling", "Rate exceeded")

    with pytest.raises(_ClientError):
        _ensure(Broken())


# ---------------------------------------------------------------------------
# 6. AssumeRole 재시도 · 만료
# ---------------------------------------------------------------------------


def test_전파_지연_거부는_재시도로_넘긴다() -> None:
    sts = FakeSts(deny_first=2)
    slept: list[float] = []
    creds = aws_role.assume_deploy_role(sts, ROLE_ARN, sleep=slept.append)
    assert sts.calls == 3 and len(slept) == 2
    assert creds.access_key_id == "ASIAFAKE" and creds.session_token == "token"
    assert creds.as_env()["AWS_SESSION_TOKEN"] == "token"


def test_끝까지_거부면_AssumeRole_거부로_알린다() -> None:
    sts = FakeSts(deny_always=True)
    with pytest.raises(aws_role.RoleSetupDenied) as info:
        aws_role.assume_deploy_role(sts, ROLE_ARN, retries=2, sleep=lambda _s: None)
    assert info.value.action == "sts:AssumeRole"
    assert sts.calls == 2


def test_만료_임박_판정은_여유_시간_기준() -> None:
    now = datetime.now(timezone.utc)
    soon = aws_role.TemporaryCredentials("k", "s", "t", now + timedelta(minutes=5))
    later = aws_role.TemporaryCredentials("k", "s", "t", now + timedelta(minutes=50))
    assert soon.expires_soon(now) is True
    assert later.expires_soon(now) is False


def test_문자열_만료_시각도_읽는다() -> None:
    class StrSts:
        def assume_role(self, **kw) -> dict:
            return {"Credentials": {
                "AccessKeyId": "a", "SecretAccessKey": "b", "SessionToken": "c",
                "Expiration": "2030-01-01T00:00:00Z",
            }}

    creds = aws_role.assume_deploy_role(StrSts(), ROLE_ARN)
    assert creds.expiration == datetime(2030, 1, 1, tzinfo=timezone.utc)


def test_요약에는_비밀_값이_없다() -> None:
    iam = FakeIam()
    result = _ensure(iam)
    creds = aws_role.assume_deploy_role(FakeSts(), ROLE_ARN)
    summary = aws_role.role_summary(result, creds)
    dumped = json.dumps(summary)
    assert "secret" not in dumped and "token" not in dumped
    assert summary["role_arn"] == ROLE_ARN and summary["created"] is True
