"""
ReCoder Core — S3 정적 배포 (BYO)

FR-05-03 「S3 배포 BYO 전환」. 지금까지는 운영자 계정의 게이트웨이가 팀
버킷에 대신 올렸다. 확정 D9(전부 BYO)에 맞춰, 사용자 자기 자격증명으로
**사용자 계정의 버킷**에 직접 올린다.

이 파일은 AWS 호출만 담당한다. 버킷 이름·키 정규화·URL 조립 같은 계산은
`core/s3_byo.py` 에 있고 그쪽에서 검사한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

try:  # 코어 단독 실행 / 패키지 실행 양쪽 지원
    import s3_byo
    from api.routes import aws as aws_routes  # type: ignore
except ImportError:  # pragma: no cover - 패키지 경로 폴백
    from core import s3_byo  # type: ignore
    from core.api.routes import aws as aws_routes  # type: ignore


def _resolve_region(explicit: str = "") -> str:
    """리전 결정 — **권한표·ECR 목록과 같은 순서**를 쓴다.

    여기만 따로 기본값을 두면 같은 세션인데 화면마다 다른 리전을 말하게 된다
    (실제로 그런 버그가 있었다: 배포 폼 ap-northeast-2 vs 자격증명 us-east-1).
    """
    import os as _os

    _, session_region, _ = aws_routes._deployment_identity()
    return (
        (explicit or "").strip()
        or session_region
        or _os.environ.get("AWS_REGION", "")
        or _os.environ.get("AWS_DEFAULT_REGION", "")
        or aws_routes.DEFAULT_REGION
    )


def _build_boto3_session(profile=None, region=None):
    return aws_routes._build_boto3_session(profile=profile, region=region)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["deploy-s3"])


class S3DeployRequest(BaseModel):
    #: 버킷 이름의 근거. 워크스페이스 폴더 이름을 그대로 보내면 된다.
    project: str
    #: [{"path": "index.html", "content": "..."}] — 확장이 읽어서 보낸다.
    files: list[dict]
    region: Optional[str] = None
    profile: Optional[str] = None


class S3DeployResponse(BaseModel):
    status: str
    bucket: str
    region: str
    url: str
    uploaded: list[str]
    bucket_created: bool
    #: index.html 이 없어 다른 HTML 을 복제했다면 그 원본 경로.
    index_copied_from: Optional[str] = None
    message: str


def _aws_error_detail(exc: Exception, action: str) -> str:
    """AWS 오류를 **다음 행동이 있는** 문장으로.

    botocore 예외를 그대로 노출하면 사용자는 무엇을 해야 할지 모른다.
    특히 권한 부족은 「권한표」를 다시 적용하면 풀리는 경우가 대부분이다.
    """
    code = ""
    try:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
    except Exception:  # noqa: BLE001
        code = ""

    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        return (
            f"{action} 권한이 없습니다. 배포 센터의 「권한표」를 사용자/역할에 "
            f"다시 적용한 뒤 시도하세요. (AWS: {code})"
        )
    if code == "BucketAlreadyExists":
        return (
            "같은 이름의 버킷이 다른 AWS 계정에 이미 있습니다. S3 버킷 이름은 "
            "전 세계에서 유일해야 합니다. 프로젝트 이름을 조금 바꿔 다시 시도하세요."
        )
    if code == "InvalidLocationConstraint":
        return (
            f"요청한 리전이 올바르지 않습니다. 자격증명이 유효한 리전과 같은지 "
            f"확인하세요. (AWS: {code})"
        )
    return f"{action} 실패: {exc}"


def _ensure_bucket(client, bucket: str, region: str) -> bool:
    """버킷이 없으면 만든다. 새로 만들었으면 True.

    이미 **내 계정에** 있는 버킷이면 그대로 쓴다(재배포). 남의 계정 버킷과
    이름이 겹치면 CreateBucket 이 BucketAlreadyExists 로 실패하고, 위
    _aws_error_detail 이 그 상황을 설명한다.
    """
    from botocore.exceptions import ClientError  # type: ignore

    try:
        client.head_bucket(Bucket=bucket)
        return False
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status not in (403, 404):
            raise
        if status == 403:
            # 존재하지만 내 것이 아니다 — 만들려 하면 더 헷갈리는 오류가 난다.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"버킷 '{bucket}' 이 이미 다른 계정에 있습니다. 프로젝트 이름을 "
                    f"바꿔 다시 시도하세요."
                ),
            ) from exc

    client.create_bucket(**s3_byo.create_bucket_kwargs(bucket, region))
    return True


def _configure_public_website(client, bucket: str) -> None:
    """정적 호스팅 + 공개 읽기.

    최신 S3 는 계정·버킷 단위로 공개 정책을 **기본 차단**한다. 차단을 풀지
    않고 PutBucketPolicy 만 하면 성공한 것처럼 보이는데 링크는 403 이 된다.
    그래서 순서가 중요하다: 차단 해제 → 정책 → 웹사이트 설정.
    """
    client.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,        # ACL 경로는 계속 막는다(정책만 사용)
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": False,     # 아래 읽기 전용 정책을 허용
            "RestrictPublicBuckets": False,
        },
    )
    client.put_bucket_policy(
        Bucket=bucket,
        Policy=json.dumps(s3_byo.public_read_policy(bucket)),
    )
    client.put_bucket_website(
        Bucket=bucket,
        WebsiteConfiguration=s3_byo.website_configuration(),
    )


def _deploy_sync(request: S3DeployRequest, region: str) -> S3DeployResponse:
    """실제 AWS 호출. 이벤트 루프를 막지 않도록 라우트가 스레드로 넘긴다."""
    from botocore.exceptions import ClientError  # type: ignore

    plan = s3_byo.plan_upload(request.files)

    session = _build_boto3_session(
        profile=(request.profile or aws_routes._effective_profile()), region=region,
    )
    try:
        account_id = session.client("sts").get_caller_identity()["Account"]
    except ClientError as exc:
        raise HTTPException(
            status_code=401, detail=_aws_error_detail(exc, "AWS 자격증명 확인"),
        ) from exc

    bucket = s3_byo.bucket_name(request.project, account_id)
    client = session.client("s3", region_name=region)

    try:
        created = _ensure_bucket(client, bucket, region)
    except ClientError as exc:
        raise HTTPException(
            status_code=502, detail=_aws_error_detail(exc, "S3 버킷 생성"),
        ) from exc

    try:
        _configure_public_website(client, bucket)
    except ClientError as exc:
        raise HTTPException(
            status_code=502, detail=_aws_error_detail(exc, "정적 호스팅 설정"),
        ) from exc

    try:
        for item in plan.items:
            client.put_object(
                Bucket=bucket,
                Key=item.key,
                Body=item.content.encode("utf-8"),
                ContentType=item.content_type,
                #: 재배포 직후 옛 화면이 뜨지 않게 캐시를 끈다. 데모에서
                #: "고쳤는데 그대로인데요" 로 시간을 버리는 걸 막는다.
                CacheControl="no-cache",
            )
    except ClientError as exc:
        raise HTTPException(
            status_code=502, detail=_aws_error_detail(exc, "파일 업로드"),
        ) from exc

    url = s3_byo.website_url(bucket, region)
    note = ""
    if plan.index_copied_from:
        note = f" index.html 이 없어 {plan.index_copied_from} 를 진입 문서로 함께 올렸습니다."
    return S3DeployResponse(
        status="deployed",
        bucket=bucket,
        region=region,
        url=url,
        uploaded=plan.keys,
        bucket_created=created,
        index_copied_from=plan.index_copied_from,
        message=f"{len(plan.items)}개 파일을 올렸습니다.{note}",
    )


@router.post("/api/deploy/s3", response_model=S3DeployResponse)
async def deploy_s3(request: S3DeployRequest) -> S3DeployResponse:
    """정적 사이트를 **사용자 자기 계정** S3 버킷에 배포하고 공개 URL 을 준다."""
    if not (request.project or "").strip():
        raise HTTPException(status_code=400, detail="project 가 비어 있습니다.")

    region = _resolve_region(request.region or "")
    if not region:
        raise HTTPException(
            status_code=400,
            detail=(
                "AWS 리전을 알 수 없습니다. 배포 폼에서 리전을 지정하거나 "
                "AWS 연결을 먼저 완료하세요."
            ),
        )

    try:
        return await asyncio.to_thread(_deploy_sync, request, region)
    except s3_byo.S3DeployError as exc:
        # 사용자 입력 문제 — 그대로 보여 준다.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("S3 BYO 배포 실패")
        raise HTTPException(
            status_code=500, detail=f"S3 배포 중 예기치 못한 오류: {exc}",
        ) from exc
