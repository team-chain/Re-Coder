"""AWS 연결이 바뀌면 AI 클라이언트 캐시와 브레이커가 새로 시작한다.

보드 이슈 「AWS 연결 후에도 AI 클라이언트가 예전 자격증명을 물고 있음 —
연결 시 캐시 무효화 필요」(2026-09-20 실기기 발견).

재현: 연결 전에 채팅을 한 번 → BedrockProvider 가 그 시점 자격증명으로
boto3 클라이언트를 만들어 캐시 → /api/aws/connect 로 새 키 → 캐시된
클라이언트는 옛 키로 계속 호출 → "연결했는데도 AI 가 안 됨".

여기서 고정하는 것
    1. connect · connect-profile · configure · clear · role/setup 성공 직후
       라우터 싱글턴이 **다른 인스턴스**로 바뀐다 (= 프로바이더 재생성).
    2. 옛 키로 실패해 열린 브레이커가 닫힌다(초기화된다).
    3. 무효화가 실패해도 연결은 성공이다 — 이 후처리는 Soft Fail.
    4. 역할 모드 자격증명 갱신도 같은 처리를 한다(옛 임시 키는 곧 만료).

boto3 는 부르지 않는다 — STS/권한 점검을 바꿔 끼우고, 라우터 싱글턴은
실제 모듈 전역을 본다(프로바이더 생성은 BedrockProvider 생성자 대역으로 막는다).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import aws_role  # noqa: E402
from api.routes import aws  # noqa: E402
from llm import breaker, provider_router, router  # noqa: E402

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
_AWS_ENV_KEYS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", aws.ENV_ASSUME_ROLE_ARN,
)


class _Sentinel:
    """LLMProviderRouter 대역 — 인스턴스 정체성만 비교하면 된다."""

    def __init__(self) -> None:
        self.created_at = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    before = {key: os.environ.get(key) for key in _AWS_ENV_KEYS}
    for key in _AWS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(aws, "_active_profile", None)
    monkeypatch.setattr(aws, "_role_state", None)
    monkeypatch.setattr(aws, "_refresh_diagnostics_cache", lambda: None)
    monkeypatch.setattr(aws, "_inspect_deploy_permissions", lambda *a, **k: None)
    monkeypatch.setattr(aws, "CREDENTIALS_FILE", Path("/nonexistent/aws_credentials.json"))
    monkeypatch.setattr(
        aws, "_call_sts_get_caller_identity",
        lambda *, profile, region: {"account": ACCOUNT, "arn": f"arn:aws:iam::{ACCOUNT}:user/dev", "user_id": "AIDA"},
    )
    #: 프로바이더 생성(boto3 클라이언트)을 막고 정체성만 남긴다.
    monkeypatch.setattr(provider_router, "LLMProviderRouter", _Sentinel)
    monkeypatch.setattr(provider_router, "_singleton", None)
    monkeypatch.setattr(router, "_router_instance", None)
    breaker.reset_all()
    yield
    breaker.reset_all()
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _prime_cache():
    """연결 전에 채팅을 한 번 시도한 상태 — 라우터가 만들어져 캐시됐다."""
    r = router.get_router()
    assert router.get_router() is r
    return r, r._pr


def _open_breaker(key: str = "bedrock:haiku") -> None:
    br = breaker.breaker_for(key)
    for _ in range(50):
        br.record_failure() if hasattr(br, "record_failure") else None
    #: 구현이 어떻게 열든, 초기화 후에는 같은 키가 **새 객체**여야 한다.


def _assert_rebuilt(before_router, before_pr, before_breaker) -> None:
    after = router.get_router()
    assert after is not before_router, "라우터가 재생성되지 않았다 — 옛 클라이언트를 계속 쓴다"
    assert after._pr is not before_pr, "provider_router 싱글턴이 그대로다"
    assert breaker.breaker_for("bedrock:haiku") is not before_breaker, "브레이커가 초기화되지 않았다"


def _profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred = tmp_path / "credentials"
    cred.write_text("[dev]\naws_access_key_id = AKIADEV0000000000\naws_secret_access_key = s\n", encoding="utf-8")
    monkeypatch.setattr(aws, "AWS_CREDENTIALS_FILE", cred)
    monkeypatch.setattr(aws, "AWS_CONFIG_FILE", tmp_path / "config")


def test_connect_가_라우터와_브레이커를_새로_만든다() -> None:
    r, pr = _prime_cache()
    br = breaker.breaker_for("bedrock:haiku")
    asyncio.run(aws.connect_aws(aws.AwsConnectRequest(
        access_key_id="AKIANEW00000000000", secret_access_key="newsecret", region=REGION,
    )))
    _assert_rebuilt(r, pr, br)


def test_connect_profile_도_같다(tmp_path, monkeypatch) -> None:
    _profiles(tmp_path, monkeypatch)
    r, pr = _prime_cache()
    br = breaker.breaker_for("bedrock:haiku")
    asyncio.run(aws.connect_aws_profile(aws.AwsProfileConnectRequest(profile="dev", region=REGION)))
    _assert_rebuilt(r, pr, br)


def test_configure_도_같다(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(aws, "CREDENTIALS_FILE", tmp_path / "aws_credentials.json")
    monkeypatch.setattr(aws, "RECODER_HOME", tmp_path)
    r, pr = _prime_cache()
    br = breaker.breaker_for("bedrock:haiku")
    asyncio.run(aws.configure_aws(aws.AwsConfigureRequest(
        access_key_id="AKIANEW00000000000", secret_access_key="newsecret", region=REGION,
    )))
    _assert_rebuilt(r, pr, br)


def test_clear_도_같다() -> None:
    r, pr = _prime_cache()
    br = breaker.breaker_for("bedrock:haiku")
    asyncio.run(aws.clear_aws())
    _assert_rebuilt(r, pr, br)


def test_role_setup_과_갱신도_같다(tmp_path, monkeypatch) -> None:
    _profiles(tmp_path, monkeypatch)

    class _Sess:
        def __init__(self, **kw): self.region_name = REGION
        def client(self, name, region_name=None):
            class _Sts:
                def get_caller_identity(self):
                    return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/dev", "UserId": "AIDA"}
            return _Sts()

    monkeypatch.setattr(aws, "_base_session", lambda p, e, r: _Sess())
    monkeypatch.setattr(aws, "_build_boto3_session", lambda profile=None, region=None: _Sess())
    monkeypatch.setattr(aws_role, "ensure_deploy_role", lambda iam, **kw: aws_role.RoleSetupResult(
        role_arn=f"arn:aws:iam::{ACCOUNT}:role/{aws_role.ROLE_NAME}", role_name=aws_role.ROLE_NAME,
        created=True, trust_updated=False, policy_statements=1, principal_arn=kw["principal_arn"],
    ))
    monkeypatch.setattr(aws_role, "assume_deploy_role", lambda sts, arn, **kw: aws_role.TemporaryCredentials(
        "ASIAROLE", "s", "t", datetime.now(timezone.utc) + timedelta(hours=1),
    ))

    r, pr = _prime_cache()
    br = breaker.breaker_for("bedrock:haiku")
    resp = asyncio.run(aws.setup_aws_role(aws.AwsRoleSetupRequest(profile="dev", region=REGION)))
    assert resp.ok is True
    _assert_rebuilt(r, pr, br)

    #: 갱신 — 옛 임시 키로 만든 클라이언트는 곧 ExpiredToken 이 되므로 다시 버린다.
    r2, pr2 = _prime_cache()
    br2 = breaker.breaker_for("bedrock:haiku")
    aws._role_state["expires_at"] = datetime.now(timezone.utc) + timedelta(minutes=1)
    aws._refresh_role_credentials()
    _assert_rebuilt(r2, pr2, br2)


def test_무효화가_실패해도_연결은_성공이다(monkeypatch) -> None:
    def boom(force_rebuild: bool = False):
        raise RuntimeError("provider init exploded")

    monkeypatch.setattr(router, "get_router", boom)
    status = asyncio.run(aws.connect_aws(aws.AwsConnectRequest(
        access_key_id="AKIANEW00000000000", secret_access_key="newsecret", region=REGION,
    )))
    assert status.ready is True


def test_후처리는_클라이언트를_먼저_버리고_진단을_돈다(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(aws, "_invalidate_llm_clients", lambda: order.append("invalidate"))
    monkeypatch.setattr(aws, "_refresh_diagnostics_cache", lambda: order.append("diagnostics"))
    aws._credentials_changed()
    assert order == ["invalidate", "diagnostics"]
