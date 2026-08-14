"""
ReCoder — S3 정적 배포 (BYO: 사용자 자기 AWS 계정)

무엇을 바꾸는가
    지금까지 S3 정적 배포는 **운영자 계정의 게이트웨이**가 팀 버킷에 대신
    올려 줬다. 확정 D9(전부 BYO)에서는 사용자 키로 **사용자 계정의 버킷**에
    직접 올려야 한다. 이 모듈은 그 경로의 순수 로직만 담는다.

왜 AWS 호출과 분리했는가
    버킷 이름 규칙 · 키 정규화 · 웹사이트 URL 조립처럼 **틀리면 조용히
    이상해지는** 계산이 대부분이다. boto3 안에 섞어 두면 자격증명 없이는
    검사할 수 없어서 아무도 안 건드리게 된다. 여기 꺼내 두면 그대로 검사한다.

참고: gateway/src/deploy.py 의 경로 sanitize · index 복제 · content-type
추론을 옮겨 왔다(같은 편의 동작을 유지해야 한다는 DoD).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

#: 게이트웨이 경로와 같은 상한. 정적 사이트 초안을 올리는 용도라 넉넉할 필요가 없고,
#: 실수로 node_modules 를 통째로 올리는 사고를 여기서 막는다.
MAX_FILES = 30
MAX_BYTES_PER_FILE = 3_000_000

#: 버킷 이름 접두사. aws_policy.py 의 RESOURCE_PREFIX 와 같아야 한다 —
#: 최소권한 정책이 `arn:aws:s3:::recoder-*` 로 범위를 좁히기 때문이다.
BUCKET_PREFIX = "recoder"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".wasm": "application/wasm",
    ".map": "application/json",
}

#: 웹사이트 엔드포인트에 **하이픈**을 쓰는 리전들.
#:
#: AWS 는 초기 리전에서 `s3-website-<region>` 를, 이후 리전에서
#: `s3-website.<region>` 를 쓴다. 이걸 틀리면 업로드는 성공하는데 링크만
#: 안 열려서, 원인을 찾는 데 한참 걸린다.
#: https://docs.aws.amazon.com/general/latest/gr/s3.html#s3_website_region_endpoints
_DASH_WEBSITE_REGIONS = frozenset({
    "us-east-1", "us-west-1", "us-west-2",
    "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
    "eu-west-1", "sa-east-1", "us-gov-west-1",
})


class S3DeployError(ValueError):
    """사용자 입력 문제 — 라우트가 400 으로 바꿔 그대로 보여 준다."""


# ---------------------------------------------------------------------------
# 파티션 (aws / aws-cn / aws-us-gov)
# ---------------------------------------------------------------------------
#
# aws_policy.py 가 이미 파티션을 구분해 ARN 을 만든다. 여기만 `arn:aws:` 와
# `amazonaws.com` 을 못 박으면, 그 정책으로는 이 경로에 도달할 수 있는데
# 정작 PutBucketPolicy 의 Resource 가 안 맞고 공개 URL 도 열리지 않는다.
# 한쪽만 파티션을 알면 나머지 한쪽에서 조용히 어긋난다.


def partition_for_region(region: str) -> str:
    """리전 → AWS 파티션 이름.

    **직접 판정하지 않고 `aws_policy.partition_for` 에 위임한다.**
    처음엔 여기에 같은 규칙을 베껴 썼는데, 격리 리전(us-iso-*, us-isob-*)을
    빠뜨려서 그 리전들이 `aws` 로 떨어졌다. 권한표는 이미 두 파티션을
    지원하므로 사용자는 그 리전에 도달할 수 있는데, 여기서 만든 ARN 만
    어떤 리소스와도 매칭되지 않는 상태가 된다.

    규칙이 두 벌 있으면 반드시 갈라진다. 한 벌만 둔다.
    """
    try:
        from aws_policy import partition_for  # type: ignore
    except ImportError:  # pragma: no cover - 패키지 경로 폴백
        from core.aws_policy import partition_for  # type: ignore
    return partition_for(region)


#: 파티션별 엔드포인트 DNS 접미사.
#:
#: 접미사를 틀리면 버킷 생성도 웹사이트 설정도 다 성공하는데 **돌려준 URL 만
#: 열리지 않는다.** 실패가 조용해서 원인을 찾기 어렵다.
_DNS_SUFFIX_BY_PARTITION = {
    "aws": "amazonaws.com",
    "aws-cn": "amazonaws.com.cn",
    "aws-us-gov": "amazonaws.com",
    "aws-iso": "c2s.ic.gov",
    "aws-iso-b": "sc2s.sgov.gov",
}


def dns_suffix_for_region(region: str) -> str:
    """리전 → 엔드포인트 DNS 접미사."""
    return _DNS_SUFFIX_BY_PARTITION.get(partition_for_region(region), "amazonaws.com")


# ---------------------------------------------------------------------------
# 이름 · 경로
# ---------------------------------------------------------------------------


def slug(name: str, fallback: str = "site") -> str:
    """S3 버킷 이름에 쓸 수 있게 다듬는다(소문자·숫자·하이픈)."""
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", (name or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:40] or fallback


def project_fingerprint(project: str) -> str:
    """프로젝트 이름의 **원문** 지문 6자.

    왜 필요한가
        슬러그는 정보를 잃는다. `a_b` 와 `a b` 는 둘 다 `a-b` 가 되고,
        한글·한자 이름은 전부 fallback(`site`)으로 뭉개지며, 앞 40자가 같은
        긴 이름들도 하나로 합쳐진다. 버킷 이름이 슬러그만으로 정해지면
        **서로 다른 프로젝트가 같은 버킷을 공유**한다. 두 번째 배포는 그걸
        재배포로 취급해 겹치는 파일을 덮어쓰고 나머지는 남긴다 — 두 사이트가
        섞인 채로 배포된다.

    hash() 를 쓰지 않는 이유: 파이썬의 문자열 해시는 프로세스마다 달라진다
    (PYTHONHASHSEED). 그러면 재배포마다 버킷이 새로 생긴다. 안정적인
    sha256 을 쓴다.
    """
    import hashlib

    raw = (project or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]


def bucket_name(project: str, account_id: str) -> str:
    """사용자 계정 안에서 쓸 배포 버킷 이름.

    형태: `recoder-<슬러그>-<프로젝트지문6>-<계정뒤6>`

    · 계정 뒤 6자리 — S3 버킷 이름은 **전 세계에서 유일**해야 한다. 프로젝트
      이름만 쓰면 남이 이미 만든 이름과 부딪혀 CreateBucket 이 실패한다.
    · 프로젝트 지문 — 슬러그가 같아지는 서로 다른 프로젝트를 갈라 놓는다
      (위 project_fingerprint 참고).

    규칙: 3~63자, 소문자·숫자·하이픈, 하이픈으로 시작/끝 금지.
    같은 (프로젝트, 계정) 이면 항상 같은 이름이어야 한다 — 재배포가 같은
    버킷으로 가야 하기 때문이다.
    """
    account_suffix = re.sub(r"\D", "", account_id or "")[-6:] or "000000"
    fingerprint = project_fingerprint(project)
    #: 63자 상한. 지문과 계정 접미사는 **유일성의 근거라 자르지 않고**,
    #: 가운데 슬러그만 줄인다.
    fixed_len = len(BUCKET_PREFIX) + 1 + 1 + len(fingerprint) + 1 + len(account_suffix)
    keep = max(1, 63 - fixed_len)
    return f"{BUCKET_PREFIX}-{slug(project)[:keep]}-{fingerprint}-{account_suffix}".strip("-")


def safe_key(path: str) -> str:
    """업로드 키 정규화.

    `..` 와 선행 `/` 를 제거해 버킷 안 다른 경로로 빠져나가지 못하게 한다.
    윈도우 구분자(`\\`)도 슬래시로 바꾼다 — 확장이 윈도우에서 경로를 만든다.
    """
    p = (path or "").replace("\\", "/").lstrip("/")
    parts = [seg for seg in p.split("/") if seg and seg not in (".", "..")]
    return "/".join(parts)


def content_type(path: str) -> str:
    """확장자 → Content-Type.

    이걸 안 붙이면 S3 가 application/octet-stream 으로 서빙해서 브라우저가
    HTML 을 렌더하지 않고 다운로드한다. "올라갔는데 페이지가 안 뜬다" 의
    흔한 원인이다.
    """
    ext = os.path.splitext(path)[1].lower()
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# 업로드 계획
# ---------------------------------------------------------------------------


@dataclass
class UploadItem:
    key: str
    content: str
    content_type: str


@dataclass
class UploadPlan:
    items: list[UploadItem] = field(default_factory=list)
    #: index.html 이 없어 다른 HTML 을 복제했다면 그 원본 키. 사용자에게 알린다.
    index_copied_from: str | None = None

    @property
    def keys(self) -> list[str]:
        return [item.key for item in self.items]


def plan_upload(files: list[dict]) -> UploadPlan:
    """올릴 파일 목록 → 업로드 계획.

    편의 동작(기존 게이트웨이 경로와 동일하게 유지):
      · index.html 이 없으면 첫 HTML 을 index.html 로도 올린다. 그래야 버킷
        루트 URL 이 바로 열린다. 이게 없으면 사용자는 링크를 받고도 403 을
        본다.
    """
    if not isinstance(files, list) or not files:
        raise S3DeployError("업로드할 파일이 없습니다.")
    if len(files) > MAX_FILES:
        raise S3DeployError(
            f"파일은 최대 {MAX_FILES}개까지 올릴 수 있습니다 (요청: {len(files)}개). "
            f"빌드 산출물 폴더만 지정했는지 확인하세요."
        )

    plan = UploadPlan()
    seen: set[str] = set()
    html_keys: list[str] = []

    for entry in files:
        raw_path = (entry or {}).get("path") or (entry or {}).get("filename") or ""
        key = safe_key(str(raw_path))
        if not key:
            raise S3DeployError(f"올릴 수 없는 파일 경로입니다: {raw_path!r}")
        content = (entry or {}).get("content")
        if not isinstance(content, str):
            raise S3DeployError(f"{key}: 파일 내용이 문자열이 아닙니다.")
        size = len(content.encode("utf-8"))
        if size > MAX_BYTES_PER_FILE:
            raise S3DeployError(
                f"{key}: 파일이 너무 큽니다 ({size:,} 바이트, 상한 "
                f"{MAX_BYTES_PER_FILE:,})."
            )
        if key in seen:
            raise S3DeployError(f"같은 경로가 두 번 들어왔습니다: {key}")
        seen.add(key)

        plan.items.append(UploadItem(key, content, content_type(key)))
        if key.lower().endswith((".html", ".htm")):
            html_keys.append(key)

    if "index.html" not in seen:
        if not html_keys:
            raise S3DeployError(
                "HTML 파일이 하나도 없습니다. 정적 사이트로 배포하려면 "
                "index.html 이 필요합니다."
            )
        source = html_keys[0]
        original = next(item for item in plan.items if item.key == source)
        plan.items.append(UploadItem("index.html", original.content, content_type("index.html")))
        plan.index_copied_from = source

    return plan


# ---------------------------------------------------------------------------
# 리전 · URL · 정책
# ---------------------------------------------------------------------------


def website_url(bucket: str, region: str) -> str:
    """정적 웹사이트 호스팅 엔드포인트 URL.

    두 가지를 리전에서 끌어낸다.
      · 구분자 — 초기 리전은 `s3-website-<region>`, 이후 리전은 `s3-website.<region>`
      · DNS 접미사 — 중국 리전만 `amazonaws.com.cn`
    둘 중 하나만 틀려도 업로드는 성공하는데 링크만 안 열린다.
    """
    separator = "-" if region in _DASH_WEBSITE_REGIONS else "."
    return f"http://{bucket}.s3-website{separator}{region}.{dns_suffix_for_region(region)}"


def create_bucket_kwargs(bucket: str, region: str) -> dict:
    """CreateBucket 인자.

    us-east-1 은 **LocationConstraint 를 주면 InvalidLocationConstraint 로
    실패한다.** 다른 리전은 반대로 주지 않으면 us-east-1 에 만들어진다.
    이 비대칭이 S3 에서 가장 자주 밟는 지뢰다.
    """
    kwargs: dict = {"Bucket": bucket}
    if region and region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    return kwargs


def public_read_policy(bucket: str, region: str = "") -> dict:
    """정적 사이트를 열 수 있게 하는 최소 버킷 정책.

    **읽기만** 허용한다. 쓰기를 열면 아무나 사이트를 덮어쓸 수 있다.

    Resource ARN 의 파티션은 리전에서 끌어낸다. `arn:aws:` 로 못 박으면
    중국 리전(aws-cn)에서 PutBucketPolicy 가 리소스를 못 맞춘다.
    """
    partition = partition_for_region(region)
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadForStaticSite",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:{partition}:s3:::{bucket}/*",
            }
        ],
    }


def website_configuration() -> dict:
    """진입 문서와 오류 문서. 오류도 index.html 로 보내 SPA 라우팅을 살린다."""
    return {
        "IndexDocument": {"Suffix": "index.html"},
        "ErrorDocument": {"Key": "index.html"},
    }
