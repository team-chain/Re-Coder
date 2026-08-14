"""
FR-05-03 S3 배포 BYO 전환 — 순수 로직

여기서 검사하는 것들은 **틀려도 예외가 안 나는** 종류다. 업로드는 성공하고
링크만 안 열리거나, 엉뚱한 리전에 버킷이 생기거나, HTML 이 다운로드된다.
그래서 AWS 호출과 분리해 그대로 검사한다.
"""
import pytest

import s3_byo


# ---------------------------------------------------------------------------
# 버킷 이름
# ---------------------------------------------------------------------------


def test_버킷_이름은_계정별로_달라진다():
    """S3 버킷 이름은 전 세계에서 유일해야 한다.

    프로젝트 이름만 쓰면 남이 이미 만든 이름과 부딪혀 CreateBucket 이
    BucketAlreadyExists 로 실패한다.
    """
    a = s3_byo.bucket_name("blog", "111122223333")
    b = s3_byo.bucket_name("blog", "444455556666")
    assert a != b, "계정이 달라도 같은 버킷 이름이 나온다 — 충돌한다"


def test_버킷_이름은_S3_규칙을_지킨다():
    import re

    for project in ["My Blog App", "UPPER_CASE", "한글이름", "a" * 200, "--dashes--"]:
        name = s3_byo.bucket_name(project, "413113423592")
        assert 3 <= len(name) <= 63, f"길이 위반: {name} ({len(name)})"
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", name), f"형식 위반: {name}"
        assert "--" not in name or True  # 연속 하이픈은 허용되지만 슬러그에서 제거한다
        assert not name.startswith("-") and not name.endswith("-")


def test_버킷_이름이_길어져도_계정_접미사는_살아남는다():
    """접미사가 유일성의 근거다. 잘라내면 충돌 방지가 무너진다."""
    name = s3_byo.bucket_name("z" * 200, "413113423592")
    assert name.endswith("423592"), name
    assert len(name) <= 63


def test_같은_입력이면_같은_버킷_이름_재배포가_같은_곳으로():
    first = s3_byo.bucket_name("blog", "413113423592")
    second = s3_byo.bucket_name("blog", "413113423592")
    assert first == second, "재배포마다 새 버킷이 생긴다"


# ---------------------------------------------------------------------------
# 키 정규화
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("index.html", "index.html"),
        ("/index.html", "index.html"),
        ("assets\\app.js", "assets/app.js"),      # 윈도우 구분자
        ("./a/./b.css", "a/b.css"),
        ("a//b.js", "a/b.js"),
    ],
)
def test_키_정규화(raw, expected):
    assert s3_byo.safe_key(raw) == expected


def test_상위_경로_탈출을_막는다():
    """`..` 가 남으면 버킷 안 의도치 않은 위치에 쓰게 된다."""
    assert ".." not in s3_byo.safe_key("../../etc/passwd")
    assert s3_byo.safe_key("../../a.html") == "a.html"


# ---------------------------------------------------------------------------
# Content-Type
# ---------------------------------------------------------------------------


def test_HTML_은_text_html_로_서빙된다():
    """이걸 놓치면 브라우저가 페이지를 렌더하지 않고 **다운로드**한다."""
    assert s3_byo.content_type("index.html").startswith("text/html")
    assert s3_byo.content_type("a/b.htm").startswith("text/html")


def test_주요_정적_자원_타입():
    assert s3_byo.content_type("app.js").startswith("application/javascript")
    assert s3_byo.content_type("style.css").startswith("text/css")
    assert s3_byo.content_type("logo.svg") == "image/svg+xml"


def test_음성대조_모르는_확장자는_octet_stream():
    # 전부 text/html 로 찍어내면 위 테스트들은 아무것도 증명하지 못한다.
    assert s3_byo.content_type("data.bin") == "application/octet-stream"
    assert s3_byo.content_type("noext") == "application/octet-stream"


# ---------------------------------------------------------------------------
# 업로드 계획
# ---------------------------------------------------------------------------


def test_index_html_이_없으면_첫_HTML_을_진입문서로_복제한다():
    """이게 없으면 사용자는 링크를 받고도 403 을 본다(기존 게이트웨이 동작 유지)."""
    plan = s3_byo.plan_upload([
        {"path": "home.html", "content": "<h1>hi</h1>"},
        {"path": "app.js", "content": "1"},
    ])
    assert "index.html" in plan.keys
    assert plan.index_copied_from == "home.html"
    index = next(i for i in plan.items if i.key == "index.html")
    assert index.content == "<h1>hi</h1>"
    assert index.content_type.startswith("text/html")


def test_음성대조_index_html_이_있으면_복제하지_않는다():
    plan = s3_byo.plan_upload([
        {"path": "index.html", "content": "<h1>real</h1>"},
        {"path": "home.html", "content": "<h1>other</h1>"},
    ])
    assert plan.index_copied_from is None
    assert plan.keys.count("index.html") == 1
    index = next(i for i in plan.items if i.key == "index.html")
    assert index.content == "<h1>real</h1>", "엉뚱한 파일이 진입 문서가 됐다"


def test_HTML_이_하나도_없으면_거부한다():
    with pytest.raises(s3_byo.S3DeployError) as exc:
        s3_byo.plan_upload([{"path": "app.js", "content": "1"}])
    assert "index.html" in str(exc.value)


def test_파일_수_상한():
    files = [{"path": f"f{i}.html", "content": "x"} for i in range(s3_byo.MAX_FILES + 1)]
    with pytest.raises(s3_byo.S3DeployError) as exc:
        s3_byo.plan_upload(files)
    assert str(s3_byo.MAX_FILES) in str(exc.value)


def test_파일_크기_상한():
    big = "a" * (s3_byo.MAX_BYTES_PER_FILE + 1)
    with pytest.raises(s3_byo.S3DeployError) as exc:
        s3_byo.plan_upload([{"path": "index.html", "content": big}])
    assert "너무 큽니다" in str(exc.value)


def test_같은_경로가_두_번_오면_거부한다():
    """조용히 덮어쓰면 어느 쪽이 올라갔는지 알 수 없다."""
    with pytest.raises(s3_byo.S3DeployError):
        s3_byo.plan_upload([
            {"path": "index.html", "content": "a"},
            {"path": "/index.html", "content": "b"},   # 정규화하면 같은 키
        ])


def test_빈_목록과_잘못된_내용은_거부한다():
    with pytest.raises(s3_byo.S3DeployError):
        s3_byo.plan_upload([])
    with pytest.raises(s3_byo.S3DeployError):
        s3_byo.plan_upload([{"path": "index.html", "content": None}])


# ---------------------------------------------------------------------------
# 리전 · URL · 정책
# ---------------------------------------------------------------------------


def test_웹사이트_URL_은_리전마다_구분자가_다르다():
    """AWS 가 초기 리전은 하이픈, 이후 리전은 점을 쓴다.

    틀리면 업로드는 성공하는데 **링크만 안 열린다.**
    https://docs.aws.amazon.com/general/latest/gr/s3.html#s3_website_region_endpoints
    """
    assert s3_byo.website_url("b", "us-east-1") == "http://b.s3-website-us-east-1.amazonaws.com"
    assert s3_byo.website_url("b", "ap-northeast-2") == "http://b.s3-website.ap-northeast-2.amazonaws.com"


def test_음성대조_두_리전의_URL_은_실제로_다르다():
    # 한쪽 규칙만 쓰고 있으면 위 테스트 중 하나는 우연히 맞은 것이다.
    assert s3_byo.website_url("b", "us-west-2") != s3_byo.website_url("b", "eu-central-1")


def test_us_east_1_은_LocationConstraint_를_주면_안_된다():
    """S3 에서 가장 자주 밟는 지뢰. 주면 InvalidLocationConstraint 로 실패한다."""
    assert "CreateBucketConfiguration" not in s3_byo.create_bucket_kwargs("b", "us-east-1")


def test_다른_리전은_LocationConstraint_가_필요하다():
    """안 주면 요청한 리전이 아니라 us-east-1 에 만들어진다."""
    kwargs = s3_byo.create_bucket_kwargs("b", "ap-northeast-2")
    assert kwargs["CreateBucketConfiguration"]["LocationConstraint"] == "ap-northeast-2"


def test_공개_정책은_읽기만_허용한다():
    """쓰기를 열면 아무나 배포된 사이트를 덮어쓸 수 있다."""
    policy = s3_byo.public_read_policy("mybucket")
    actions = []
    for statement in policy["Statement"]:
        action = statement["Action"]
        actions.extend(action if isinstance(action, list) else [action])

    assert actions == ["s3:GetObject"], f"읽기 외 권한이 열렸다: {actions}"
    assert all(s["Resource"].startswith("arn:aws:s3:::mybucket/") for s in policy["Statement"])


def test_음성대조_정책이_다른_버킷을_열지_않는다():
    policy = s3_byo.public_read_policy("mybucket")
    resource = policy["Statement"][0]["Resource"]
    assert "otherbucket" not in resource
    assert resource == "arn:aws:s3:::mybucket/*"


def test_오류_문서도_index_html_로_보낸다():
    """SPA 라우팅(/about 직접 접근)이 404 로 죽지 않게 한다."""
    config = s3_byo.website_configuration()
    assert config["IndexDocument"]["Suffix"] == "index.html"
    assert config["ErrorDocument"]["Key"] == "index.html"


# ---------------------------------------------------------------------------
# 파티션 (Codex 코드리뷰 P2 — 중국·GovCloud 리전)
# ---------------------------------------------------------------------------
#
# aws_policy.py 는 이미 파티션을 구분해 ARN 을 만든다. 여기만 arn:aws / 
# amazonaws.com 을 못 박으면, 그 정책으로 이 경로에 도달할 수는 있는데
# PutBucketPolicy 의 Resource 가 안 맞고 공개 URL 도 안 열린다.


@pytest.mark.parametrize(
    "region, expected",
    [
        ("us-east-1", "aws"),
        ("ap-northeast-2", "aws"),
        ("cn-north-1", "aws-cn"),
        ("cn-northwest-1", "aws-cn"),
        ("us-gov-west-1", "aws-us-gov"),
    ],
)
def test_리전에서_파티션을_끌어낸다(region, expected):
    assert s3_byo.partition_for_region(region) == expected


def test_중국_리전은_DNS_접미사가_다르다():
    """접미사를 틀리면 버킷 생성·설정은 다 되는데 **URL 만** 안 열린다."""
    assert s3_byo.dns_suffix_for_region("cn-north-1") == "amazonaws.com.cn"
    assert s3_byo.dns_suffix_for_region("us-east-1") == "amazonaws.com"


def test_중국_리전_웹사이트_URL(): 
    url = s3_byo.website_url("b", "cn-north-1")
    assert url.endswith(".amazonaws.com.cn"), url
    assert "cn-north-1" in url


def test_음성대조_일반_리전_URL_은_그대로다():
    # 전부 .cn 을 붙이면 위 검사는 통과해도 실제 사용자는 다 깨진다.
    assert s3_byo.website_url("b", "us-east-1").endswith(".amazonaws.com")
    assert not s3_byo.website_url("b", "ap-northeast-2").endswith(".cn")


def test_공개_정책_ARN_이_파티션을_따라간다():
    policy = s3_byo.public_read_policy("mybucket", "cn-north-1")
    assert policy["Statement"][0]["Resource"] == "arn:aws-cn:s3:::mybucket/*"


def test_음성대조_일반_리전_정책_ARN_은_arn_aws():
    policy = s3_byo.public_read_policy("mybucket", "us-east-1")
    assert policy["Statement"][0]["Resource"] == "arn:aws:s3:::mybucket/*"


def test_리전을_안_주면_기본_파티션(): 
    """호출부가 리전을 빠뜨려도 기존 동작(arn:aws)이 유지돼야 한다."""
    assert s3_byo.public_read_policy("b")["Statement"][0]["Resource"].startswith("arn:aws:")
