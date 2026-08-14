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


def _session_and_region(profile: str = "", explicit_region: str = ""):
    """(boto3 세션, 리전). **둘을 같은 프로필에서 뽑는다.**

    예전에는 리전을 `_deployment_identity()` 에서 가져왔는데, 그건 전역
    활성 프로필을 본다. 요청이 `profile` 을 지정하면 **자격증명은 그 프로필,
    리전은 다른 프로필**이 되어, 다른 리전용으로 설정된 프로필이 엉뚱한
    리전에 조용히 배포한다. 세션을 먼저 만들고 그 세션의 리전을 쓴다.

    우선순위: 요청이 준 리전 > 그 프로필의 리전 > 환경변수 > 기본값.
    """
    import os as _os

    resolved_profile = (profile or "").strip() or aws_routes._effective_profile()
    explicit = (explicit_region or "").strip()

    session = aws_routes._build_boto3_session(
        profile=resolved_profile, region=explicit or None,
    )
    region = explicit or (getattr(session, "region_name", "") or "").strip()
    if not region:
        region = (
            _os.environ.get("AWS_REGION", "")
            or _os.environ.get("AWS_DEFAULT_REGION", "")
            or aws_routes.DEFAULT_REGION
        )
        # 리전 없이 만든 세션은 클라이언트가 리전을 못 잡는다 — 다시 만든다.
        session = aws_routes._build_boto3_session(profile=resolved_profile, region=region)
    return session, region

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


def _bucket_region(client, bucket: str, fallback: str) -> str:
    """이미 있는 버킷이 **실제로 어느 리전에 있는지**.

    GetBucketLocation 은 us-east-1 을 `None`(또는 빈 문자열)로 돌려준다 —
    이 API 의 오래된 관례다. 그대로 쓰면 리전이 비어 URL 이 깨진다.
    """
    try:
        raw = client.get_bucket_location(Bucket=bucket).get("LocationConstraint")
    except Exception:  # noqa: BLE001 - 못 읽으면 요청 리전을 그대로 쓴다
        return fallback
    return (raw or "us-east-1")


def _ensure_bucket(client, bucket: str, region: str) -> tuple[bool, str]:
    """(새로 만들었나, 이 버킷이 실제로 있는 리전).

    이미 **내 계정에** 있는 버킷이면 그대로 쓴다(재배포). 이때 **그 버킷의
    실제 리전을 확인한다.** 버킷 이름은 리전과 무관하게 유일하므로, 처음
    us-east-1 에 만든 뒤 나중에 다른 리전을 골라 배포하면 head_bucket 은
    그냥 성공한다. 그러면 업로드는 리다이렉트로 되는데 **돌려주는 URL 만
    엉뚱한 리전을 가리켜** 열리지 않는다.

    남의 계정 버킷과 이름이 겹치면 CreateBucket 이 BucketAlreadyExists 로
    실패하고, _aws_error_detail 이 그 상황을 설명한다.
    """
    from botocore.exceptions import ClientError  # type: ignore

    try:
        client.head_bucket(Bucket=bucket)
        return False, _bucket_region(client, bucket, region)
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
    return True, region


def _configure_public_website(client, bucket: str, region: str) -> None:
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
        Policy=json.dumps(s3_byo.public_read_policy(bucket, region)),
    )
    client.put_bucket_website(
        Bucket=bucket,
        WebsiteConfiguration=s3_byo.website_configuration(),
    )


def _deploy_sync(request: S3DeployRequest, session, region: str) -> S3DeployResponse:
    """실제 AWS 호출. 이벤트 루프를 막지 않도록 라우트가 스레드로 넘긴다."""
    from botocore.exceptions import BotoCoreError, ClientError  # type: ignore

    plan = s3_byo.plan_upload(request.files)

    try:
        account_id = session.client("sts").get_caller_identity()["Account"]
    except (ClientError, BotoCoreError) as exc:
        # 자격증명이 아예 없으면 botocore 는 NoCredentialsError 를 던진다.
        # 그건 ClientError 가 **아니라** BotoCoreError 라, 예전에는 이 핸들러를
        # 그냥 지나쳐 바깥 catch-all 의 500 + 원문 예외로 나갔다. 정작 사용자가
        # 해야 할 일(AWS 연결)은 아무 데도 안 적혀 있었다.
        raise HTTPException(
            status_code=401, detail=_aws_error_detail(exc, "AWS 자격증명 확인"),
        ) from exc

    bucket = s3_byo.bucket_name(request.project, account_id)
    client = session.client("s3", region_name=region)

    try:
        created, actual_region = _ensure_bucket(client, bucket, region)
    except ClientError as exc:
        raise HTTPException(
            status_code=502, detail=_aws_error_detail(exc, "S3 버킷 생성"),
        ) from exc

    region_note = ""
    if actual_region != region:
        # 요청한 리전이 아니라 **버킷이 실제로 있는 리전**을 기준으로 삼는다.
        # 그래야 돌려주는 URL 이 열린다. 사용자에게도 알린다 — 모르면 "왜
        # 다른 리전이지" 로 헤맨다.
        region_note = (
            f" 이 버킷은 이미 {actual_region} 에 있어 그 리전으로 배포했습니다"
            f"(요청: {region}). 다른 리전에 올리려면 프로젝트 이름을 바꾸세요."
        )
        region = actual_region
        client = session.client("s3", region_name=region)

    try:
        _configure_public_website(client, bucket, region)
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
        message=f"{len(plan.items)}개 파일을 올렸습니다.{note}{region_note}",
    )


@router.post("/api/deploy/s3", response_model=S3DeployResponse)
async def deploy_s3(request: S3DeployRequest) -> S3DeployResponse:
    """정적 사이트를 **사용자 자기 계정** S3 버킷에 배포하고 공개 URL 을 준다."""
    if not (request.project or "").strip():
        raise HTTPException(status_code=400, detail="project 가 비어 있습니다.")

    session, region = _session_and_region(request.profile or "", request.region or "")
    if not region:
        raise HTTPException(
            status_code=400,
            detail=(
                "AWS 리전을 알 수 없습니다. 배포 폼에서 리전을 지정하거나 "
                "AWS 연결을 먼저 완료하세요."
            ),
        )

    try:
        return await asyncio.to_thread(_deploy_sync, request, session, region)
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
