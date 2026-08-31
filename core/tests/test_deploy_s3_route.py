"""
FR-05-03 S3 배포 BYO 전환 — 라우트 통합 (moto)

순수 로직 테스트(test_s3_byo.py)가 계산을 지키고, 여기서는 **실제 S3 API 를
올바른 순서·인자로 부르는지**를 본다. 특히:

  · 공개 차단(Public Access Block) 을 풀지 않으면 PutBucketPolicy 는 성공하는데
    링크만 403 이 된다. "성공했다는데 안 열려요" 의 원인.
  · index.html 이 없을 때의 복제가 실제 객체로 올라가는지.
  · 재배포가 같은 버킷을 재사용하는지(매번 새 버킷이 생기면 비용·혼란).

moto 가 없으면 스킵하지 않고 **실패**시킨다 — 조용히 건너뛰면 초록으로
보이지만 아무것도 검사하지 않은 상태가 된다(requirements-dev.txt 참고).
"""
import pytest
from fastapi.testclient import TestClient

import main

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

TOKEN = "t" * 32
REGION = "us-east-1"


@pytest.fixture()
def aws_env(monkeypatch):
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture()
def client(aws_env):
    app = main.create_app()
    app.state.session_token = TOKEN
    return TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 5555))


def _deploy(client, **overrides):
    body = {
        "project": "demo-site",
        "region": REGION,
        "files": [
            {"path": "index.html", "content": "<h1>hello</h1>"},
            {"path": "assets/app.js", "content": "console.log(1)"},
        ],
    }
    body.update(overrides)
    return client.post("/api/deploy/s3", json=body, headers={"X-Session-Token": TOKEN})


@mock_aws
def test_사용자_계정_버킷에_올라가고_공개_URL_을_돌려준다(client):
    """DoD: 정적 앱이 사용자 계정 S3 에 올라가고 공개 URL 로 열림."""
    resp = _deploy(client)

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["status"] == "deployed"
    assert body["bucket_created"] is True
    assert body["url"].startswith("http://") and body["bucket"] in body["url"]
    assert set(body["uploaded"]) == {"index.html", "assets/app.js"}

    s3 = boto3.client("s3", region_name=REGION)
    got = s3.get_object(Bucket=body["bucket"], Key="index.html")
    assert got["Body"].read().decode() == "<h1>hello</h1>"
    assert got["ContentType"].startswith("text/html"), (
        "HTML 이 text/html 로 안 올라갔다 — 브라우저가 렌더 대신 다운로드한다"
    )


@mock_aws
def test_공개_차단을_풀고_읽기_전용_정책을_건다(client):
    """차단을 안 풀면 정책은 걸리는데 링크는 403 이 된다."""
    import json

    bucket = _deploy(client).json()["bucket"]
    s3 = boto3.client("s3", region_name=REGION)

    block = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
    assert block["BlockPublicPolicy"] is False, "공개 정책이 여전히 차단돼 링크가 403 이 된다"
    assert block["RestrictPublicBuckets"] is False

    policy = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    actions = [s["Action"] for s in policy["Statement"]]
    assert actions == ["s3:GetObject"], f"읽기 외 권한이 열렸다: {actions}"


@mock_aws
def test_음성대조_ACL_공개는_계속_막아_둔다(client):
    """정책으로만 공개한다. ACL 경로까지 열면 필요 이상으로 넓어진다."""
    bucket = _deploy(client).json()["bucket"]
    s3 = boto3.client("s3", region_name=REGION)
    block = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
    assert block["BlockPublicAcls"] is True
    assert block["IgnorePublicAcls"] is True


@mock_aws
def test_정적_웹사이트_호스팅이_켜진다(client):
    bucket = _deploy(client).json()["bucket"]
    s3 = boto3.client("s3", region_name=REGION)
    website = s3.get_bucket_website(Bucket=bucket)
    assert website["IndexDocument"]["Suffix"] == "index.html"
    assert website["ErrorDocument"]["Key"] == "index.html"


@mock_aws
def test_index_html_이_없으면_첫_HTML_이_진입문서로_함께_올라간다(client):
    resp = _deploy(client, files=[
        {"path": "home.html", "content": "<h1>home</h1>"},
        {"path": "style.css", "content": "body{}"},
    ])

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["index_copied_from"] == "home.html"
    assert "index.html" in body["uploaded"]

    s3 = boto3.client("s3", region_name=REGION)
    got = s3.get_object(Bucket=body["bucket"], Key="index.html")
    assert got["Body"].read().decode() == "<h1>home</h1>"


@mock_aws
def test_재배포는_같은_버킷을_다시_쓴다(client):
    """매번 새 버킷이 생기면 계정에 쓰레기가 쌓이고 URL 도 바뀐다."""
    first = _deploy(client).json()
    second = _deploy(client, files=[{"path": "index.html", "content": "<h1>v2</h1>"}]).json()

    assert first["bucket"] == second["bucket"]
    assert second["bucket_created"] is False, "재배포인데 버킷을 새로 만들었다"

    s3 = boto3.client("s3", region_name=REGION)
    got = s3.get_object(Bucket=second["bucket"], Key="index.html")
    assert got["Body"].read().decode() == "<h1>v2</h1>", "새 내용으로 안 바뀌었다"


@mock_aws
def test_재배포는_새_업로드에_없는_옛_자산을_지운다(client):
    """번들이 바뀌거나 파일을 지워도 이전 공개 객체가 남으면 안 된다."""
    from botocore.exceptions import ClientError

    first = _deploy(client).json()
    second = _deploy(client, files=[{"path": "index.html", "content": "<h1>v2</h1>"}]).json()
    assert first["bucket"] == second["bucket"]
    assert "이전 배포 파일 1개" in second["message"]

    s3 = boto3.client("s3", region_name=REGION)
    with pytest.raises(ClientError) as exc:
        s3.head_object(Bucket=second["bucket"], Key="assets/app.js")
    assert exc.value.response["Error"]["Code"] in {"404", "NoSuchKey"}


@mock_aws
def test_재배포_직후_옛_화면이_뜨지_않게_캐시를_끈다(client):
    bucket = _deploy(client).json()["bucket"]
    s3 = boto3.client("s3", region_name=REGION)
    got = s3.get_object(Bucket=bucket, Key="index.html")
    assert got.get("CacheControl") == "no-cache"


@mock_aws
def test_잘못된_입력은_400_으로_이유를_알려준다(client):
    resp = _deploy(client, files=[{"path": "app.js", "content": "1"}])
    assert resp.status_code == 400
    assert "index.html" in resp.json()["detail"]


@mock_aws
def test_project_가_비면_400(client):
    resp = _deploy(client, project="   ")
    assert resp.status_code == 400
    assert "project" in resp.json()["detail"]


@mock_aws
def test_음성대조_정상_배포는_오류_필드를_달지_않는다(client):
    """전역 예외 핸들러나 오류 경로가 정상 응답까지 오염시키지 않는지."""
    body = _deploy(client).json()
    assert "detail" not in body
    assert body["index_copied_from"] is None


# ---------------------------------------------------------------------------
# Codex 코드리뷰 P2 — 프로필·자격증명·기존 버킷 리전
# ---------------------------------------------------------------------------


def test_리전은_요청한_프로필에서_뽑는다(monkeypatch):
    """자격증명과 리전이 **다른 프로필**에서 오면 엉뚱한 리전에 배포된다.

    예전에는 리전을 `_deployment_identity()` 로 구했는데, 그건 전역 활성
    프로필을 본다. 요청이 profile 을 지정하면 자격증명은 그 프로필, 리전은
    다른 프로필이 되어, 다른 리전용으로 설정된 프로필이 **조용히** 엉뚱한
    리전에 올린다.
    """
    from api.routes import aws as aws_routes
    from api.routes import deploy_s3

    seen: dict[str, object] = {}

    class _Session:
        def __init__(self, profile, region):
            self.profile = profile
            #: 프로필이 자기 리전을 들고 있는 상황을 흉내낸다.
            self.region_name = region or ("eu-central-1" if profile == "lab" else "us-east-1")

    def _fake_build(profile=None, region=None):
        seen["profile"] = profile
        return _Session(profile, region)

    monkeypatch.setattr(aws_routes, "_build_boto3_session", _fake_build)
    monkeypatch.setattr(aws_routes, "_effective_profile", lambda: "default-profile")

    session, region = deploy_s3._session_and_region("lab", "")

    assert seen["profile"] == "lab", "요청한 프로필로 세션을 안 만들었다"
    assert region == "eu-central-1", (
        f"자격증명은 lab 프로필인데 리전이 {region} 이다 — 다른 프로필의 리전을 쓴 것"
    )
    assert session.profile == "lab"


def test_음성대조_요청이_리전을_주면_그게_이긴다(monkeypatch):
    """프로필 리전이 항상 이기면 사용자가 리전을 고를 수 없다."""
    from api.routes import aws as aws_routes
    from api.routes import deploy_s3

    class _Session:
        def __init__(self, region):
            self.region_name = region or "eu-central-1"

    monkeypatch.setattr(
        aws_routes, "_build_boto3_session", lambda profile=None, region=None: _Session(region),
    )
    monkeypatch.setattr(aws_routes, "_effective_profile", lambda: "")

    _, region = deploy_s3._session_and_region("lab", "ap-northeast-2")
    assert region == "ap-northeast-2"


@mock_aws
def test_자격증명이_없으면_500이_아니라_401과_할_일을_알려준다(client, monkeypatch):
    """NoCredentialsError 는 ClientError 가 아니라 BotoCoreError 다.

    예전에는 그래서 핸들러를 지나쳐 바깥 catch-all 의 500 + 원문 예외로
    나갔고, 정작 사용자가 해야 할 일(AWS 연결)은 어디에도 없었다.
    """
    from botocore.exceptions import NoCredentialsError

    from api.routes import deploy_s3

    class _Sts:
        def get_caller_identity(self):
            raise NoCredentialsError()

    class _Session:
        region_name = REGION

        def client(self, name, **_kw):
            if name == "sts":
                return _Sts()
            raise AssertionError(f"sts 이후로 진행되면 안 된다: {name}")

    monkeypatch.setattr(
        deploy_s3, "_session_and_region", lambda *_a, **_k: (_Session(), REGION),
    )

    resp = _deploy(client)
    assert resp.status_code == 401, f"{resp.status_code} {resp.text[:200]}"
    assert "자격증명" in resp.json()["detail"]


@mock_aws
def test_이미_다른_리전에_있는_버킷이면_그_리전_URL_을_돌려준다(client):
    """버킷 이름은 리전과 무관하게 유일하다.

    처음 us-east-1 에 만든 뒤 다른 리전으로 배포하면 head_bucket 은 그냥
    성공한다. 요청 리전으로 URL 을 만들면 **그 주소에는 이 버킷이 없다.**
    """
    first = _deploy(client).json()          # us-east-1 에 생성
    bucket = first["bucket"]

    second = _deploy(client, region="ap-northeast-2").json()

    assert second["bucket"] == bucket
    assert second["region"] == "us-east-1", (
        f"버킷은 us-east-1 에 있는데 응답 리전이 {second['region']} 이다"
    )
    assert "us-east-1" in second["url"], second["url"]
    assert "ap-northeast-2" not in second["url"]
    assert "us-east-1" in second["message"], "리전이 바뀐 사실을 사용자에게 안 알렸다"


@mock_aws
def test_음성대조_같은_리전_재배포는_안내를_덧붙이지_않는다(client):
    """항상 안내가 붙으면 위 검사는 의미가 없고 사용자는 문구를 무시하게 된다."""
    _deploy(client)
    again = _deploy(client).json()
    assert again["region"] == REGION
    assert "이미" not in again["message"], again["message"]
