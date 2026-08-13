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
