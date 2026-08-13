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
# 이름 · 경로
# ---------------------------------------------------------------------------


def slug(name: str, fallback: str = "site") -> str:
    """S3 버킷 이름에 쓸 수 있게 다듬는다(소문자·숫자·하이픈)."""
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", (name or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:40] or fallback


def bucket_name(project: str, account_id: str) -> str:
    """사용자 계정 안에서 쓸 배포 버킷 이름.

    S3 버킷 이름은 **전 세계에서 유일**해야 한다. 프로젝트 이름만 쓰면
    다른 사람이 이미 만든 이름과 부딪혀 CreateBucket 이 실패한다. 계정 번호
    뒤 6자리를 붙여 충돌을 피한다(계정 번호 전체는 길이 상한에 걸린다).

    규칙: 3~63자, 소문자·숫자·하이픈, 하이픈으로 시작/끝 금지.
    """
    account_suffix = re.sub(r"\D", "", account_id or "")[-6:] or "000000"
    base = f"{BUCKET_PREFIX}-{slug(project)}-{account_suffix}"
    #: 63자 상한. 접미사는 유일성의 근거라 **자르지 않고**, 가운데 슬러그를 줄인다.
    if len(base) > 63:
        keep = 63 - len(BUCKET_PREFIX) - len(account_suffix) - 2
        base = f"{BUCKET_PREFIX}-{slug(project)[:keep]}-{account_suffix}"
    return base.strip("-")


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
    """정적 웹사이트 호스팅 엔드포인트 URL."""
    separator = "-" if region in _DASH_WEBSITE_REGIONS else "."
    return f"http://{bucket}.s3-website{separator}{region}.amazonaws.com"


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


def public_read_policy(bucket: str) -> dict:
    """정적 사이트를 열 수 있게 하는 최소 버킷 정책.

    **읽기만** 허용한다. 쓰기를 열면 아무나 사이트를 덮어쓸 수 있다.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadForStaticSite",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/*",
            }
        ],
    }


def website_configuration() -> dict:
    """진입 문서와 오류 문서. 오류도 index.html 로 보내 SPA 라우팅을 살린다."""
    return {
        "IndexDocument": {"Suffix": "index.html"},
        "ErrorDocument": {"Key": "index.html"},
    }
