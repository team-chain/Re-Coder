"""
ReCoder Core — AWS Credentials & Status Routes (§S-2 보강)

AWS Deploy Ready 활성화를 위한 자격증명 관리 엔드포인트.
- /api/aws/status: STS GetCallerIdentity 로 현재 자격증명 검증
- /api/aws/connect: 자격증명을 저장하지 않고 STS로 검증 (VS Code SecretStorage용)
- /api/aws/configure: 레거시 호환용 자격증명 저장 + 즉시 검증
- /api/aws/clear: 저장된 자격증명 제거
- /api/aws/profiles: ~/.aws/credentials 의 profile 목록
- /api/aws/ecr/repos: ECR 레포지토리 목록 (자격증명 sanity-check 용)

저장 위치:
- 기본: ~/.recoder/aws_credentials.json (0600)
- 옵션: ~/.aws/credentials [recoder] profile (저장 방식: storage="aws_credentials_file")

보안:
- 모든 응답에서 access_key_id 는 마지막 4자리만 노출
- secret_access_key 는 절대 응답에 포함하지 않음
- 자격증명 저장 직후 first_run.check_aws_deploy_ready() 재실행하여
  diagnostics.json 의 aws_deploy_ready 값을 갱신한다.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import stat
import sys
from urllib.parse import unquote
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .deploy_ecs import DEFAULT_CLUSTER as ECS_DEFAULT_CLUSTER
from .deploy_ecs import DEFAULT_SERVICE as ECS_DEFAULT_SERVICE
from .deploy_ecs import DEFAULT_TASK_FAMILY as ECS_DEFAULT_TASK_FAMILY

try:  # main.py 스택(core 를 sys.path 로) / 패키지 실행 양쪽 지원
    import aws_policy
    import aws_role
except ImportError:  # pragma: no cover
    from core import aws_policy
    from core import aws_role

logger = logging.getLogger(__name__)

router = APIRouter(tags=["aws"])

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

RECODER_HOME = Path(os.getenv("RECODER_HOME", str(Path.home() / ".recoder")))
CREDENTIALS_FILE = RECODER_HOME / "aws_credentials.json"
AWS_CREDENTIALS_FILE = Path.home() / ".aws" / "credentials"
AWS_CONFIG_FILE = Path.home() / ".aws" / "config"

DEFAULT_REGION = (
    os.getenv("AWS_REGION")
    or os.getenv("AWS_DEFAULT_REGION")
    or "ap-northeast-2"
)

# In-process cache of the most recently-stored profile name; used so that
# subsequent boto3 sessions in this process pick up the right profile even
# before the diagnostics cache is rebuilt.
_active_profile: Optional[str] = None

#: 확장이 코어를 띄울 때 넘기는 환경변수 — 값이 있으면 코어는 기반 자격증명
#: (AWS_PROFILE 또는 키)으로 이 역할을 빌려 그 자격증명만 쓴다. 역할 ARN 은
#: 비밀이 아니라 globalState 에 둔다 (비밀은 SecretStorage 에만).
ENV_ASSUME_ROLE_ARN = "RECODER_ASSUME_ROLE_ARN"

#: 역할 모드의 프로세스 내 상태. 비밀은 임시 자격증명(만료됨)뿐이고, 갱신에
#: 필요한 기반은 프로필 이름 또는 코어 시작 시 받은 키(메모리)다.
#:   role_arn, base_profile, base_env(dict|None), region, expires_at(datetime),
#:   principal_arn
_role_state: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AwsConfigureRequest(BaseModel):
    """자격증명 저장 요청.

    storage:
      - "recoder"          → ~/.recoder/aws_credentials.json 에 0600 저장 (기본)
      - "aws_credentials_file" → ~/.aws/credentials 의 [profile] 섹션에 추가
    """

    access_key_id: str = Field(..., min_length=16, max_length=128)
    secret_access_key: str = Field(..., min_length=8, max_length=256)
    region: str = ""
    profile: str = "recoder"
    storage: str = "recoder"  # "recoder" | "aws_credentials_file"
    session_token: str = ""   # 임시 자격증명용 (선택)


class AwsDeploymentPermissionContext(BaseModel):
    """IAM Simulator에 전달할 실제 ECS 배포 대상.

    연결 시점에는 아직 ECS 이름을 모를 수 있으므로 빈 값은 환경변수에서
    보완한다. 이름이 끝까지 없으면 해당 리소스 한정 액션은 "권한 없음"으로
    오판하지 않고, 배포 설정을 입력한 후 다시 점검하도록 안내한다.
    """

    ecr_repo: str = ""
    ecs_cluster: str = ""
    ecs_service: str = ""
    task_family: str = ""
    aws_region: str = ""
    task_execution_role: str = ""
    task_role: str = ""


class AwsConnectRequest(BaseModel):
    """VS Code SecretStorage에 보관하기 전, STS로 키만 검증하는 요청.

    이 경로는 파일이나 Core 설정에 자격증명을 기록하지 않는다. 검증에 성공한
    자격증명은 현재 Core 프로세스 메모리에만 유지하며, 재시작 후에는 Extension이
    VS Code SecretStorage에서 환경변수로 다시 주입한다.
    """

    access_key_id: str = Field(..., min_length=16, max_length=128)
    secret_access_key: str = Field(..., min_length=8, max_length=256)
    region: str = ""
    session_token: str = ""
    deployment_context: Optional[AwsDeploymentPermissionContext] = None


class AwsPermissionCheckRequest(BaseModel):
    """이미 연결된 키를 실제 배포 대상 기준으로 다시 검사하는 요청."""

    deployment_context: Optional[AwsDeploymentPermissionContext] = None


class AwsProfileConnectRequest(BaseModel):
    """~/.aws 에 이미 구성된 프로필로 연결하는 요청 — 키를 입력받지 않는다.

    자격증명은 사용자의 ~/.aws 파일(또는 SSO 캐시)에 이미 있다. 여기서는
    프로필 이름만 받아 STS 로 검증한 뒤 현재 Core 프로세스에 적용한다.
    이 요청에는 비밀 값이 전혀 실리지 않으므로 화면·로그 어디에도 키가
    지나가지 않는다.
    """

    profile: str = Field(..., min_length=1, max_length=128)
    region: str = ""
    deployment_context: Optional[AwsDeploymentPermissionContext] = None


class AwsIdentity(BaseModel):
    account: str = ""
    arn: str = ""
    user_id: str = ""


class AwsPermissionCheck(BaseModel):
    """배포에 필요한 IAM 권한을 읽기 전용으로 점검한 결과."""

    inspected: bool = False
    #: STS assumed-role ARN에서 IAM 역할 경로를 복원하지 못해 시뮬레이션만
    #: 완료할 수 없는 경우. 명시적인 부족 권한이 없으면 UI는 안내 후 배포를
    #: 진행할 수 있다.
    advisory_only: bool = False
    required_actions: list[str] = Field(default_factory=list)
    missing_actions: list[str] = Field(default_factory=list)
    excessive_policies: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AwsStatus(BaseModel):
    ready: bool = False
    identity: Optional[AwsIdentity] = None
    region: str = ""
    profile: str = ""
    access_key_last4: str = ""
    storage: str = ""        # "recoder" | "aws_credentials_file" | "env" | "assumed_role" | ""
    message: str = ""
    permission_check: Optional[AwsPermissionCheck] = None
    #: 역할 모드일 때만 채워진다 — 빌린 역할과 임시 자격증명 만료 시각.
    role_arn: str = ""
    expires_at: str = ""


class AwsRoleSetupRequest(BaseModel):
    """프로그램 안에서 최소권한 역할을 만들고 빌리는 요청.

    profile 을 주면 그 ~/.aws 프로필이 기반 자격증명이다. 비우면 지금 코어에
    연결된 자격증명(키 또는 프로필)을 기반으로 쓴다. 비밀 값은 실리지 않는다.
    """

    profile: str = ""
    region: str = ""
    deployment_context: Optional[AwsDeploymentPermissionContext] = None


class AwsRoleSetupResponse(BaseModel):
    """ok=False 여도 200 이다 — 권한 부족은 실패가 아니라 **콘솔 폴백 분기**다."""

    ok: bool
    mode: str                  # "role" | "console_fallback"
    message: str
    denied_action: str = ""    # 폴백일 때 어느 권한이 없었나
    role: Optional[dict[str, Any]] = None
    status: Optional[AwsStatus] = None


# ECS Fargate 배포를 실제로 시작하기 위한 최소 작업 목록. 이 목록은 배포
# 가능 여부를 결정하므로, 실제 정책은 대상 ECR 리포지토리/Task Role로 더
# 좁혀야 한다.
REQUIRED_DEPLOY_ACTIONS = [
    "ecr:GetAuthorizationToken",
    "ecr:CreateRepository",
    "ecr:DescribeRepositories",
    "ecr:BatchCheckLayerAvailability",
    "ecr:InitiateLayerUpload",
    "ecr:UploadLayerPart",
    "ecr:CompleteLayerUpload",
    "ecr:PutImage",
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
    "ecs:DescribeClusters",
    "ecs:CreateCluster",
    "ecs:DescribeServices",
    "ecs:RegisterTaskDefinition",
    "ecs:UpdateService",
    "ecs:CreateService",
    "ecs:ListTasks",
    "ecs:DescribeTasks",
    "iam:GetRole",
    "iam:PassRole",
    "logs:DescribeLogGroups",
    "logs:CreateLogGroup",
    "ec2:DescribeVpcs",
    "ec2:DescribeSubnets",
    "ec2:DescribeRouteTables",
    "ec2:DescribeSecurityGroups",
    "ec2:DescribeNetworkInterfaces",
    "ec2:CreateSecurityGroup",
    "ec2:AuthorizeSecurityGroupIngress",
]

# 비용 누적을 줄이는 설정이다. 실제 파이프라인도 이 두 호출의 실패를 경고로
# 기록한 뒤 배포를 계속하므로, 여기서 권한이 없다고 ECS 배포 자체를 막으면
# 안 된다. 다만 사용자가 비용 제어를 보완할 수 있게 결과에는 경고를 남긴다.
OPTIONAL_COST_CONTROL_ACTIONS = {
    "ecr:PutLifecyclePolicy",
    "logs:PutRetentionPolicy",
}

# ECS 서비스 연결 역할은 계정에 아직 없을 때만 CreateService 과정에서 필요하다.
# 이미 존재하는 계정에서는 이 권한 없이도 정상 배포되므로, 거부되더라도
# 배포 차단이 아닌 조건부 안내로만 보여 준다.
OPTIONAL_CONDITIONAL_ACTIONS = {
    "iam:CreateServiceLinkedRole",
}

_BROAD_POLICY_NAMES = {
    "administratoraccess",
    "poweruseraccess",
    "iamfullaccess",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask_key(key: str) -> str:
    """access_key 의 마지막 4자리만 노출."""
    if not key:
        return ""
    if len(key) <= 4:
        return "*" * len(key)
    return key[-4:]


def _set_file_permissions_secure(path: Path) -> None:
    """0600 권한 설정 (Windows 는 icacls Soft Fail)."""
    try:
        if sys.platform == "win32":
            try:
                os.system(
                    f'icacls "{path}" /inheritance:r '
                    f'/grant:r "%USERNAME%:F" >nul 2>&1'
                )
            except Exception:
                pass
        else:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except Exception as exc:  # noqa: BLE001
        logger.warning("[aws] chmod 0600 failed for %s: %s", path, exc)


def _load_stored_credentials() -> Optional[dict[str, Any]]:
    """~/.recoder/aws_credentials.json 에서 저장된 자격증명 로드."""
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[aws] credentials read failed: %s", exc)
        return None


def _save_recoder_credentials(
    access_key_id: str,
    secret_access_key: str,
    region: str,
    profile: str,
    session_token: str = "",
) -> None:
    """~/.recoder/aws_credentials.json 에 0600 으로 저장."""
    RECODER_HOME.mkdir(parents=True, exist_ok=True)

    payload = {
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "region": region or DEFAULT_REGION,
        "profile": profile or "recoder",
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "storage": "recoder",
    }
    if session_token:
        payload["session_token"] = session_token

    with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    _set_file_permissions_secure(CREDENTIALS_FILE)


def _save_aws_credentials_file(
    access_key_id: str,
    secret_access_key: str,
    region: str,
    profile: str,
    session_token: str = "",
) -> None:
    """~/.aws/credentials 의 [profile] 섹션에 추가."""
    AWS_CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)

    cp = configparser.RawConfigParser()
    if AWS_CREDENTIALS_FILE.exists():
        try:
            cp.read(AWS_CREDENTIALS_FILE, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[aws] credentials parse failed: %s", exc)

    section = profile or "recoder"
    if not cp.has_section(section):
        cp.add_section(section)
    cp.set(section, "aws_access_key_id", access_key_id)
    cp.set(section, "aws_secret_access_key", secret_access_key)
    if session_token:
        cp.set(section, "aws_session_token", session_token)

    with open(AWS_CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        cp.write(f)
    _set_file_permissions_secure(AWS_CREDENTIALS_FILE)

    # ~/.aws/config 에 region 도 같이 등록 (profile 이 'default' 가 아니면 'profile <name>' 헤더)
    if region:
        cfg = configparser.RawConfigParser()
        if AWS_CONFIG_FILE.exists():
            try:
                cfg.read(AWS_CONFIG_FILE, encoding="utf-8")
            except Exception:
                pass
        cfg_section = section if section == "default" else f"profile {section}"
        if not cfg.has_section(cfg_section):
            cfg.add_section(cfg_section)
        cfg.set(cfg_section, "region", region)
        with open(AWS_CONFIG_FILE, "w", encoding="utf-8") as f:
            cfg.write(f)
        _set_file_permissions_secure(AWS_CONFIG_FILE)


def _apply_to_process_env(
    access_key_id: str,
    secret_access_key: str,
    region: str,
    profile: str,
    session_token: str = "",
) -> None:
    """현재 프로세스의 환경변수에 자격증명을 즉시 적용.

    다음 boto3.Session() 호출이 새 자격증명을 인식하도록 한다.
    AWS_PROFILE 가 set 되어 있으면 ~/.aws/credentials 를 통해 해결되고,
    그렇지 않으면 환경변수가 우선한다.
    """
    global _active_profile
    _active_profile = profile or None

    os.environ["AWS_ACCESS_KEY_ID"] = access_key_id
    os.environ["AWS_SECRET_ACCESS_KEY"] = secret_access_key
    if session_token:
        os.environ["AWS_SESSION_TOKEN"] = session_token
    else:
        os.environ.pop("AWS_SESSION_TOKEN", None)
    if region:
        os.environ["AWS_DEFAULT_REGION"] = region
        os.environ["AWS_REGION"] = region
    if profile:
        os.environ["AWS_PROFILE"] = profile


def _build_boto3_session(profile: Optional[str] = None, region: Optional[str] = None):
    """boto3.Session 생성 — profile/region 우선순위 적용."""
    try:
        import boto3  # type: ignore
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="boto3 패키지가 설치되어 있지 않습니다. 'pip install boto3' 후 다시 시도하세요.",
        ) from exc

    kwargs: dict[str, Any] = {}
    if profile:
        kwargs["profile_name"] = profile
    if region:
        kwargs["region_name"] = region

    try:
        return boto3.Session(**kwargs)
    except Exception as exc:  # noqa: BLE001
        # profile 이 잘못된 경우 friendly 메시지
        msg = str(exc)
        if "could not be found" in msg.lower() or "ProfileNotFound" in msg:
            raise HTTPException(
                status_code=400,
                detail=f"AWS profile '{profile}' 을(를) 찾을 수 없습니다.",
            ) from exc
        raise HTTPException(status_code=500, detail=f"boto3 세션 생성 실패: {exc}") from exc


def _call_sts_get_caller_identity(profile: Optional[str], region: str) -> dict[str, str]:
    """STS GetCallerIdentity 호출 — 자격증명 검증.

    실패 시 HTTPException(401/403/500) 발생. 성공 시 {account, arn, user_id} 반환.
    """
    session = _build_boto3_session(profile=profile, region=region)
    try:
        sts = session.client("sts", region_name=region)
        identity = sts.get_caller_identity()
        return {
            "account": identity.get("Account", ""),
            "arn": identity.get("Arn", ""),
            "user_id": identity.get("UserId", ""),
        }
    except Exception as exc:  # noqa: BLE001
        # botocore.ClientError 등은 friendly 메시지로 변환
        msg = str(exc)
        # 흔한 케이스 매핑
        if "InvalidClientTokenId" in msg:
            raise HTTPException(
                status_code=401,
                detail="AWS access key 가 유효하지 않습니다 (InvalidClientTokenId).",
            ) from exc
        if "SignatureDoesNotMatch" in msg:
            raise HTTPException(
                status_code=401,
                detail="AWS secret key 가 일치하지 않습니다 (SignatureDoesNotMatch).",
            ) from exc
        if "ExpiredToken" in msg:
            raise HTTPException(
                status_code=401,
                detail="AWS 임시 자격증명이 만료되었습니다 (ExpiredToken).",
            ) from exc
        if "AccessDenied" in msg or "NotAuthorized" in msg:
            raise HTTPException(
                status_code=403,
                detail=f"sts:GetCallerIdentity 권한이 거부되었습니다: {msg}",
            ) from exc
        if "Unable to locate credentials" in msg or "NoCredentialsError" in msg:
            raise HTTPException(
                status_code=400,
                detail="AWS 자격증명을 찾을 수 없습니다. /api/aws/configure 로 먼저 등록하세요.",
            ) from exc
        raise HTTPException(status_code=500, detail=f"STS 호출 실패: {msg}") from exc


def _detect_credential_source() -> tuple[str, str]:
    """현재 boto3 가 어떤 소스에서 자격증명을 잡고 있는지 추정.

    반환: (storage_label, profile_name)
    storage_label: "recoder" | "aws_credentials_file" | "env" | "assumed_role" | ""
    """
    if _role_state is not None:
        #: 환경변수에 있는 건 빌린 역할의 임시 자격증명이다 — "env" 로 보이면
        #: 화면이 키 연결로 착각한다.
        return "assumed_role", str(_role_state.get("base_profile") or "")
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        # 환경변수가 우선이지만 우리가 _apply_to_process_env 로 세팅했을 수도 있음
        # → 저장 파일 존재 여부로 구분
        if CREDENTIALS_FILE.exists():
            return "recoder", _active_profile or "recoder"
        if AWS_CREDENTIALS_FILE.exists() and _active_profile:
            return "aws_credentials_file", _active_profile
        return "env", _active_profile or ""
    if CREDENTIALS_FILE.exists():
        return "recoder", _active_profile or "recoder"
    if AWS_CREDENTIALS_FILE.exists():
        return "aws_credentials_file", _active_profile or "default"
    return "", ""


def _iam_principal_arn(identity_arn: str) -> Optional[str]:
    """STS ARN을 IAM 시뮬레이션에 사용할 수 있는 사용자/역할 ARN으로 변환한다."""
    if ":iam:" in identity_arn and (":user/" in identity_arn or ":role/" in identity_arn):
        return identity_arn
    # arn:aws:sts::123456789012:assumed-role/role-name/session-name
    marker = ":assumed-role/"
    if ":sts:" in identity_arn and marker in identity_arn:
        prefix, role_and_session = identity_arn.split(marker, 1)
        role_name = role_and_session.rsplit("/", 1)[0]
        account = prefix.split(":")[4]
        partition = prefix.split(":")[1]
        return f"arn:{partition}:iam::{account}:role/{role_name}"
    return None


def _assumed_role_name(identity_arn: str) -> Optional[str]:
    """STS assumed-role ARN에서 역할의 마지막 이름만 읽는다.

    STS ARN에는 IAM 역할 path가 없으므로, 이 이름으로 GetRole을 시도해 실제
    IAM ARN을 얻는다. 읽기 권한이 없으면 호출자는 계속 배포할 수 있으므로
    이후 시뮬레이션 결과를 advisory로만 취급할 수 있게 ``None``이 아니다.
    """
    marker = ":assumed-role/"
    if ":sts:" not in identity_arn or marker not in identity_arn:
        return None
    role_and_session = identity_arn.split(marker, 1)[1]
    role_name = role_and_session.rsplit("/", 1)[0]
    return role_name.rsplit("/", 1)[-1] or None


def _policy_is_administrator(document: Any) -> bool:
    """관리형/인라인 정책 문서가 사실상 전체 권한인지 판별한다."""
    if isinstance(document, str):
        try:
            document = json.loads(unquote(document))
        except (TypeError, ValueError):
            return False
    if not isinstance(document, dict):
        return False
    statements = document.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            continue
        actions = statement.get("Action", [])
        resources = statement.get("Resource", [])
        if isinstance(actions, str):
            actions = [actions]
        if isinstance(resources, str):
            resources = [resources]
        if "*" in actions and "*" in resources:
            return True
    return False


def _simulation_partition(identity_arn: str) -> str:
    """STS ARN의 partition을 보존한다 (일반 aws / GovCloud / 중국 리전)."""
    parts = (identity_arn or "").split(":", 2)
    return parts[1] if len(parts) > 1 and parts[0] == "arn" else "aws"


def _resolved_permission_context(
    context: Optional[AwsDeploymentPermissionContext],
) -> tuple[Optional[AwsDeploymentPermissionContext], Optional[str]]:
    """명시 입력 → 실제 ECS 요청 기본값 순으로 검사 대상을 정한다."""
    supplied = context or AwsDeploymentPermissionContext()
    try:
        execution_role, task_role = aws_policy.resolve_roles(
            supplied.task_execution_role,
            supplied.task_role,
        )
    except ValueError as exc:
        # configured_execution_role()도 같은 환경변수를 다시 파싱하므로 여기서
        # 재호출하면 똑같은 ValueError가 다시 난다. 키(STS)는 유효할 수 있으니
        # 연결을 실패시키지 않고 점검 불완전 상태와 설정 안내를 반환한다.
        return None, f"ECS 역할 설정이 올바르지 않아 권한 점검을 완료하지 못했습니다: {exc}"
    # 확장 ECS 어댑터가 쓰는 기본값과 **같은 값**을 먼저 확정한다. 빈 값을
    # 환경변수에서 읽으면 점검은 통과했는데 실제 배포는 recoder-* 대상으로
    # 나가는 두 개의 서로 다른 계약이 생긴다.
    ecs_cluster = (supplied.ecs_cluster or ECS_DEFAULT_CLUSTER).strip()
    ecs_service = (supplied.ecs_service or ECS_DEFAULT_SERVICE).strip()
    return AwsDeploymentPermissionContext(
        # ECS 배포 경로는 ECR_REPOSITORY 환경변수를 읽지 않고
        # ECSDeployRequest.repo_name(기본 recoder-app)을 쓴다. 여기에서만
        # ECR_REPOSITORY를 보면 권한 점검은 통과했는데 실제 push가 다른
        # 저장소로 나가 실패하는 상태가 된다.
        # ECSAgent.ecr_repo_name()과 같다. repo_name을 보내지 않은 확장 요청은
        # service 이름으로 ECR 리포지토리를 정하므로 recoder-app을 고정하면 안
        # 된다.
        ecr_repo=(supplied.ecr_repo or ecs_service).strip(),
        ecs_cluster=ecs_cluster,
        ecs_service=ecs_service,
        task_family=(supplied.task_family or ECS_DEFAULT_TASK_FAMILY).strip(),
        aws_region=(supplied.aws_region or "").strip(),
        task_execution_role=execution_role,
        task_role=task_role,
    ), None


def _inspect_deploy_permissions(
    identity: dict[str, str],
    region: str,
    deployment_context: Optional[AwsDeploymentPermissionContext] = None,
) -> AwsPermissionCheck:
    """실제 ECS 대상 ARN과 조건을 넣어 IAM 권한을 읽기 전용 점검한다.

    ``simulate_principal_policy``에 ResourceArns 없이 액션만 넘기면 IAM은
    리소스 한정 정책을 wildcard 대상에 대입한다. 그러면 정상적인 ECR/ECS
    최소권한 정책도 거부로 나올 수 있다. 따라서 리포지토리·클러스터·서비스·
    역할 ARN을 액션 종류별로 분리하고, PassRole에는 정책과 같은 ECS 조건을
    함께 전달한다.
    """
    report = AwsPermissionCheck(required_actions=list(REQUIRED_DEPLOY_ACTIONS))
    identity_arn = identity.get("arn", "")
    principal_arn = _iam_principal_arn(identity_arn)
    if not principal_arn:
        report.warnings.append("IAM 사용자 또는 역할 ARN을 확인할 수 없어 권한 점검을 건너뛰었습니다.")
        return report

    context, context_error = _resolved_permission_context(deployment_context)
    if context_error or context is None:
        report.warnings.append(context_error or "ECS 배포 설정을 확인할 수 없습니다.")
        return report
    simulation_region = (context.aws_region or region).strip()
    try:
        session = _build_boto3_session(region=simulation_region)
        iam = session.client("iam", region_name=simulation_region)
    except Exception as exc:  # noqa: BLE001
        report.warnings.append(f"IAM 권한 점검을 시작할 수 없습니다: {exc}")
        return report

    # assumed-role ARN에는 `/team/Deployer` 같은 IAM 역할 path가 빠진다.
    # GetRole을 허용한 계정에서는 정확한 ARN으로 바꿔 시뮬레이션한다. 이 읽기
    # 권한이 없는 계정도 배포 자체는 가능하므로, 아래에서 시뮬레이션이 전부
    # 실패한 경우에만 advisory로 돌린다.
    unresolved_assumed_role_path = False
    assumed_role_name = _assumed_role_name(identity_arn)
    if assumed_role_name:
        try:
            resolved_arn = str(iam.get_role(RoleName=assumed_role_name)["Role"]["Arn"])
            if resolved_arn:
                principal_arn = resolved_arn
        except Exception as exc:  # noqa: BLE001
            unresolved_assumed_role_path = True
            logger.info("[aws] assumed-role IAM path lookup unavailable: %s", exc)

    account_id = identity.get("account", "") or principal_arn.split(":")[4]
    partition = _simulation_partition(identity.get("arn", ""))
    if not account_id or not simulation_region:
        report.warnings.append("AWS 계정 또는 리전 정보를 확인할 수 없어 리소스별 권한 점검을 건너뛰었습니다.")
        return report

    # Extension ECS 어댑터는 caller 계정의 ECR client가 돌려준 repositoryUri로
    # 이미지를 올린다. 별도 ecr_registry 입력값은 실제 배포에 쓰이지 않으므로
    # 여기서만 다른 계정을 시뮬레이션하지 않는다.
    ecr_arn = f"arn:{partition}:ecr:{simulation_region}:{account_id}:repository/{context.ecr_repo}"
    role_arns = [
        f"arn:{partition}:iam::{account_id}:role/{context.task_execution_role}",
    ]
    if context.task_role and context.task_role != context.task_execution_role:
        role_arns.append(f"arn:{partition}:iam::{account_id}:role/{context.task_role}")

    # ResourceArns는 액션별로만 보낸다. 모든 ARN을 한 요청에 섞으면 ECR 액션을
    # ECS ARN에, PassRole을 ECR ARN에 대입하는 교차 조합이 되어 다시 오판한다.
    simulations: list[tuple[list[str], list[str], Optional[list[dict[str, object]]]]] = [
        ([
            # ecs:DescribeTaskDefinition 은 여기 있었지만 뺐다. 권한표가 더는
            # 주지 않는 액션을 시뮬레이션하면 implicitDeny 가 나오고, 아래에서
            # missing_actions 에 그대로 실려 **배포가 통째로 막힌다.**
            # 시뮬레이션 목록은 권한표가 주는 범위를 넘어서면 안 된다.
            "ecr:GetAuthorizationToken", "ecs:RegisterTaskDefinition",
            "logs:DescribeLogGroups",
            "ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeRouteTables",
            "ec2:DescribeSecurityGroups", "ec2:DescribeNetworkInterfaces",
        ], ["*"], None),
        ([
            "ecr:CreateRepository", "ecr:DescribeRepositories",
            "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
            "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
            "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:PutLifecyclePolicy",
        ], [ecr_arn], None),
        # PreflightAgent는 실행 역할 존재를 확인한다. task role은 PassRole만
        # 필요하므로 GetRole 대상에 불필요하게 추가하지 않는다.
        (["iam:GetRole"], [role_arns[0]], None),
        (["iam:PassRole"], role_arns, [{
            "ContextKeyName": "iam:PassedToService",
            "ContextKeyValues": ["ecs-tasks.amazonaws.com"],
            "ContextKeyType": "string",
        }]),
    ]
    cluster_arn = f"arn:{partition}:ecs:{simulation_region}:{account_id}:cluster/{context.ecs_cluster}"
    service_arn = (
        f"arn:{partition}:ecs:{simulation_region}:{account_id}:service/"
        f"{context.ecs_cluster}/{context.ecs_service}"
    )
    task_arn = f"arn:{partition}:ecs:{simulation_region}:{account_id}:task/{context.ecs_cluster}/*"
    # ECSAgent.log_group_name()은 task_definition_family에서 이 경로를
    # 결정한다. 와일드카드를 넣으면 실제 한 그룹에만 준 최소권한 정책이
    # implicitDeny로 오판된다.
    log_group_arn = (
        f"arn:{partition}:logs:{simulation_region}:{account_id}:"
        f"log-group:/ecs/{context.task_family}"
    )
    vpc_arn = f"arn:{partition}:ec2:{simulation_region}:{account_id}:vpc/*"
    security_group_arn = f"arn:{partition}:ec2:{simulation_region}:{account_id}:security-group/*"
    # Extension 요청의 provision 기본값은 true다. 아래는 배포가 실제로
    # 확보하는 클러스터·로그 그룹·네트워크·보안 그룹·서비스의 권한을 같은
    # 리소스 문맥으로 검사한다.
    simulations.extend([
        (["ecs:DescribeClusters", "ecs:CreateCluster"], [cluster_arn], None),
        (["ecs:DescribeServices", "ecs:UpdateService", "ecs:CreateService"], [service_arn], None),
        (["ecs:ListTasks"], ["*"], [{
            "ContextKeyName": "ecs:cluster",
            "ContextKeyValues": [cluster_arn],
            "ContextKeyType": "string",
        }]),
        (["ecs:DescribeTasks"], [task_arn], None),
        (["iam:CreateServiceLinkedRole"], ["*"], [{
            "ContextKeyName": "iam:AWSServiceName",
            "ContextKeyValues": ["ecs.amazonaws.com"],
            "ContextKeyType": "string",
        }]),
        (["logs:CreateLogGroup", "logs:PutRetentionPolicy"], [log_group_arn], None),
        (["ec2:CreateSecurityGroup"], [vpc_arn, security_group_arn], None),
        (["ec2:AuthorizeSecurityGroupIngress"], [security_group_arn], None),
    ])
    deferred_actions: list[str] = []

    decisions: dict[str, list[str]] = {}
    simulated_actions: list[str] = []
    failed_actions: list[str] = []
    for actions, resource_arns, context_entries in simulations:
        try:
            params: dict[str, object] = {
                "PolicySourceArn": principal_arn,
                "ActionNames": actions,
                "ResourceArns": resource_arns,
            }
            if context_entries:
                params["ContextEntries"] = context_entries
            simulation = iam.simulate_principal_policy(**params)
            simulated_actions.extend(actions)
            for item in simulation.get("EvaluationResults", []):
                action = item.get("EvalActionName", "")
                decisions.setdefault(action, []).append(item.get("EvalDecision", "implicitDeny"))
        except Exception as exc:  # noqa: BLE001
            failed_actions.extend(actions)
            logger.info("[aws] IAM permission simulation unavailable for %s: %s", actions, exc)

    denied_actions = [
        action for action in simulated_actions
        if any(decision.lower() != "allowed" for decision in decisions.get(action, ["implicitDeny"]))
    ]
    report.missing_actions = [
        action for action in denied_actions
        if action not in OPTIONAL_COST_CONTROL_ACTIONS
        and action not in OPTIONAL_CONDITIONAL_ACTIONS
    ]
    optional_cost_actions = [
        action for action in denied_actions
        if action in OPTIONAL_COST_CONTROL_ACTIONS
    ]
    optional_conditional_actions = [
        action for action in denied_actions
        if action in OPTIONAL_CONDITIONAL_ACTIONS
    ]
    # 일부 그룹만 확인했거나 IAM Simulator 호출이 하나라도 실패했다면, 이
    # 결과는 "점검 완료"가 아니다. UI는 inspected + missing 없음일 때만
    # 초록 완료를 표시하므로, 여기서 엄격하게 완료 여부를 구분한다.
    report.inspected = (
        not failed_actions
        and not deferred_actions
        and set(REQUIRED_DEPLOY_ACTIONS).issubset(simulated_actions)
    )
    if unresolved_assumed_role_path and failed_actions and not report.missing_actions:
        report.advisory_only = True
    if report.missing_actions:
        report.warnings.append("ECS 배포에 필요한 권한 일부가 실제 배포 대상에서 허용되지 않았습니다.")
    if optional_cost_actions:
        report.warnings.append(
            "배포는 가능하지만 비용 최적화 설정 권한이 없습니다: "
            f"{', '.join(optional_cost_actions)}. "
            "ECR 이미지 자동 정리 또는 CloudWatch 로그 보존기간 설정이 적용되지 않을 수 있습니다."
        )
    if optional_conditional_actions:
        report.warnings.append(
            "ECS 서비스 연결 역할 생성 권한이 없습니다. 계정에 "
            "AWSServiceRoleForECS가 이미 있으면 배포는 계속할 수 있고, "
            "없다면 AWS 콘솔에서 ECS를 한 번 열거나 해당 권한을 추가하세요."
        )
    if report.advisory_only:
        report.warnings.append(
            "현재 자격증명은 IAM 역할 경로가 있는 assumed-role일 수 있어 권한 "
            "시뮬레이션을 완료하지 못했습니다. 명시적으로 거부된 권한은 없어 "
            "배포를 계속할 수 있지만, AWS에서 권한 오류가 나면 역할 정책을 확인하세요."
        )
    if failed_actions:
        report.warnings.append("IAM 권한 시뮬레이션 권한이 없어 일부 배포 권한을 자동 확인하지 못했습니다.")
    if deferred_actions:
        report.warnings.append(
            "ECS 클러스터·서비스가 아직 설정되지 않아 "
            f"{', '.join(deferred_actions)} 권한은 배포 설정 입력 후 다시 점검해야 합니다."
        )

    # 명백히 과도한 AWS 관리형 정책을 확인한다. 정책 본문을 읽을 권한이 없더라도
    # 이름만으로 확실한 정책은 표시한다.
    try:
        if ":user/" in principal_arn:
            user_name = principal_arn.rsplit("/", 1)[-1]
            attached = iam.list_attached_user_policies(UserName=user_name).get("AttachedPolicies", [])
            inline_names = iam.list_user_policies(UserName=user_name).get("PolicyNames", [])
            inline_documents = [
                (name, iam.get_user_policy(UserName=user_name, PolicyName=name).get("PolicyDocument"))
                for name in inline_names
            ]
        else:
            role_name = principal_arn.rsplit("/", 1)[-1]
            attached = iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
            inline_names = iam.list_role_policies(RoleName=role_name).get("PolicyNames", [])
            inline_documents = [
                (name, iam.get_role_policy(RoleName=role_name, PolicyName=name).get("PolicyDocument"))
                for name in inline_names
            ]
        for policy in attached:
            name = str(policy.get("PolicyName", ""))
            arn = str(policy.get("PolicyArn", ""))
            if name.lower() in _BROAD_POLICY_NAMES:
                report.excessive_policies.append(name)
                continue
            try:
                metadata = iam.get_policy(PolicyArn=arn).get("Policy", {})
                version_id = metadata.get("DefaultVersionId")
                if version_id:
                    version = iam.get_policy_version(PolicyArn=arn, VersionId=version_id)
                    if _policy_is_administrator(version.get("PolicyVersion", {}).get("Document")):
                        report.excessive_policies.append(name or arn)
            except Exception:  # 정책 본문 읽기는 선택 점검이다.
                continue
        for name, document in inline_documents:
            if _policy_is_administrator(document):
                report.excessive_policies.append(f"인라인 정책: {name}")
    except Exception as exc:  # noqa: BLE001
        report.warnings.append("연결된 IAM 정책 목록을 읽을 권한이 없어 과다 권한 점검이 제한됩니다.")
        logger.info("[aws] IAM policy listing unavailable: %s", exc)

    report.excessive_policies = list(dict.fromkeys(report.excessive_policies))
    if report.excessive_policies:
        report.warnings.append("관리자급 또는 전체 권한 정책이 감지되었습니다. 배포 전용 최소권한 키를 권장합니다.")
    return report


def _refresh_diagnostics_cache() -> None:
    """자격증명 저장/삭제 후 first_run 의 aws_deploy_ready 진단을 즉시 재실행.

    실패해도 자격증명 저장 자체는 성공으로 처리한다 (Soft Fail).
    """
    try:
        # late import: first_run 이 schemas 를 import 하므로 circular 위험 회피
        from first_run import check_aws_deploy_ready, load_diagnostics, save_diagnostics

        cached = load_diagnostics()
        new_status, issues = check_aws_deploy_ready()

        if cached is None:
            # 진단 결과가 아직 없음 → 부분 갱신만 수행할 수 없으니 skip
            return

        cached.aws_deploy_ready = new_status
        # aws_deploy_ready 관련 issue 만 교체. 기존 다른 issue 는 유지.
        other_issues = [
            i for i in (cached.issues or []) if not i.startswith("AWS Deploy Ready")
        ]
        cached.issues = other_issues + issues
        cached.validation_time = datetime.now(timezone.utc).isoformat()
        save_diagnostics(cached)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[aws] diagnostics refresh failed: %s", exc)


def _load_into_process_if_needed() -> None:
    """프로세스 시작 후 ~/.recoder/aws_credentials.json 가 있으면 env 에 주입.

    Core 가 재시작된 경우 환경변수가 비어있을 수 있으므로 status 조회 시점에
    한번 더 시도한다.
    """
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        _enter_role_mode_from_env()
        return
    stored = _load_stored_credentials()
    if not stored:
        _enter_role_mode_from_env()
        return
    _apply_to_process_env(
        access_key_id=stored.get("access_key_id", ""),
        secret_access_key=stored.get("secret_access_key", ""),
        region=stored.get("region", DEFAULT_REGION),
        profile=stored.get("profile", "recoder"),
        session_token=stored.get("session_token", ""),
    )
    _enter_role_mode_from_env()


def _environment_snapshot() -> tuple[dict[str, Optional[str]], Optional[str]]:
    """검증용 임시 환경변수를 원상 복구하기 위한 스냅샷."""
    return (
        {
            "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID"),
            "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY"),
            "AWS_SESSION_TOKEN": os.environ.get("AWS_SESSION_TOKEN"),
            "AWS_REGION": os.environ.get("AWS_REGION"),
            "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION"),
            "AWS_PROFILE": os.environ.get("AWS_PROFILE"),
        },
        _active_profile,
    )


def _restore_environment(snapshot: dict[str, Optional[str]], profile: Optional[str]) -> None:
    """_environment_snapshot()으로 만든 상태를 정확히 복구한다."""
    global _active_profile
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _active_profile = profile


# ---------------------------------------------------------------------------
# 역할 모드 — 프로그램 안에서 만든 최소권한 역할을 빌려 쓴다 (aws_role)
# ---------------------------------------------------------------------------


def _base_credentials_from_env() -> Optional[dict[str, str]]:
    """지금 환경변수에 있는 키를 갱신용 기반으로 떠 둔다. 역할 모드로 바뀌면
    환경변수는 임시 자격증명으로 덮이므로, 그 전에 한 번 떠야 한다."""
    key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    if not key or not secret:
        return None
    base = {"AWS_ACCESS_KEY_ID": key, "AWS_SECRET_ACCESS_KEY": secret}
    token = os.environ.get("AWS_SESSION_TOKEN", "")
    if token:
        base["AWS_SESSION_TOKEN"] = token
    return base


def _base_session(base_profile: str, base_env: Optional[dict[str, str]], region: str):
    """기반 자격증명으로 boto3 세션을 만든다 — 임시 자격증명이 환경변수에
    덮여 있어도 영향받지 않게 프로필/키를 **명시**한다."""
    import boto3  # type: ignore

    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    if base_profile:
        kwargs["profile_name"] = base_profile
    elif base_env:
        kwargs["aws_access_key_id"] = base_env["AWS_ACCESS_KEY_ID"]
        kwargs["aws_secret_access_key"] = base_env["AWS_SECRET_ACCESS_KEY"]
        if base_env.get("AWS_SESSION_TOKEN"):
            kwargs["aws_session_token"] = base_env["AWS_SESSION_TOKEN"]
    else:
        raise HTTPException(
            status_code=400,
            detail="역할을 만들 기반 자격증명이 없습니다. 프로필을 고르거나 키로 먼저 연결하세요.",
        )
    return boto3.Session(**kwargs)


def _apply_role_credentials(creds: "aws_role.TemporaryCredentials", region: str) -> None:
    """빌린 임시 자격증명을 코어 프로세스에 적용한다.

    AWS_PROFILE 은 **지운다** — 남겨두면 boto3 가 어느 쪽을 쓰는지 사람이
    헷갈린다(실제로는 키가 이기지만, 화면과 로그가 프로필을 가리킨다).
    """
    global _active_profile
    _active_profile = None
    os.environ.pop("AWS_PROFILE", None)
    os.environ.update(creds.as_env())
    if region:
        os.environ["AWS_DEFAULT_REGION"] = region
        os.environ["AWS_REGION"] = region


def _refresh_role_credentials() -> None:
    """역할 모드에서 임시 자격증명이 만료에 가까우면 다시 빌린다.

    실패하면 상태를 지우지 않는다 — 다음 status 조회에서 다시 시도하고,
    만료가 지나면 STS 검증이 ExpiredToken 으로 알려 준다.
    """
    global _role_state
    state = _role_state
    if state is None:
        return
    expires_at = state.get("expires_at")
    if isinstance(expires_at, datetime):
        remaining = expires_at - datetime.now(timezone.utc)
        if remaining > aws_role.REFRESH_MARGIN:
            return
    try:
        session = _base_session(
            str(state.get("base_profile") or ""), state.get("base_env"), str(state.get("region") or ""),
        )
        creds = aws_role.assume_deploy_role(
            session.client("sts", region_name=str(state.get("region") or "") or None),
            str(state["role_arn"]), retries=1,
        )
        _apply_role_credentials(creds, str(state.get("region") or ""))
        state["expires_at"] = creds.expiration
    except Exception as exc:  # noqa: BLE001
        logger.warning("[aws] 역할 자격증명 갱신 실패: %s", exc)


def _enter_role_mode_from_env() -> None:
    """코어 시작 시 RECODER_ASSUME_ROLE_ARN 이 있으면 역할을 빌려 그걸로 바꾼다.

    확장은 재시작마다 기반(프로필 이름 또는 키)과 역할 ARN 만 넘긴다. 실패해도
    기반 자격증명은 그대로 남으니 배포는 되지만 범위가 좁혀지지 않은 상태다 —
    status 메시지로 알린다.
    """
    global _role_state
    if _role_state is not None:
        return
    role_arn = (os.environ.get(ENV_ASSUME_ROLE_ARN) or "").strip()
    if not role_arn:
        return
    base_profile = (os.environ.get("AWS_PROFILE") or "").strip()
    base_env = None if base_profile else _base_credentials_from_env()
    if not base_profile and not base_env:
        return
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION
    )
    try:
        session = _base_session(base_profile, base_env, region)
        creds = aws_role.assume_deploy_role(
            session.client("sts", region_name=region), role_arn, retries=1,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[aws] 시작 시 역할 빌리기 실패 — 기반 자격증명으로 계속: %s", exc)
        return
    _apply_role_credentials(creds, region)
    _role_state = {
        "role_arn": role_arn,
        "base_profile": base_profile,
        "base_env": base_env,
        "region": region,
        "expires_at": creds.expiration,
        "principal_arn": "",
    }


def _leave_role_mode(clear_env: bool = True) -> None:
    """역할 모드를 끝낸다. clear_env=False 는 "기반이 바뀌었을 뿐 역할 지시는
    남긴다"는 뜻 — 코어 시작 시 기반 적용 → 역할 진입 순서에서 쓴다."""
    global _role_state
    _role_state = None
    if clear_env:
        os.environ.pop(ENV_ASSUME_ROLE_ARN, None)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/aws/connect", response_model=AwsStatus)
async def connect_aws(req: AwsConnectRequest) -> AwsStatus:
    """AWS 키를 검증하고 현재 Core 프로세스에만 적용한다.

    키는 어떤 파일에도 저장하지 않는다. 실패한 요청은 기존 환경을 되돌리지만,
    성공한 요청은 Extension이 SecretStorage에 보관하기 전에도 현재 세션에서
    즉시 배포 진단을 실행할 수 있도록 메모리에 유지한다.
    """
    region = (req.region or DEFAULT_REGION).strip()
    snapshot, prior_profile = _environment_snapshot()
    try:
        # boto3의 표준 credential chain을 그대로 써서 임시/장기 자격증명 모두 검증한다.
        _apply_to_process_env(
            access_key_id=req.access_key_id,
            secret_access_key=req.secret_access_key,
            region=region,
            profile="",
            session_token=req.session_token,
        )
        identity = _call_sts_get_caller_identity(profile=None, region=region)
        permission_check = _inspect_deploy_permissions(
            identity, region, req.deployment_context,
        )
    except Exception:
        _restore_environment(snapshot, prior_profile)
        raise

    #: 새 기반으로 연결됐다 — 이전에 빌린 역할 상태는 이 기반의 것이 아니다.
    _leave_role_mode()

    # SecretStorage 기반 연결도 기존 configure 경로와 똑같이 진단 캐시를
    # 갱신해야 한다. 그렇지 않으면 연결은 성공했는데 AWS Deploy Ready가
    # 연결 전 결과를 계속 표시하는 상태 불일치가 생긴다.
    _refresh_diagnostics_cache()

    return AwsStatus(
        ready=True,
        identity=AwsIdentity(**identity),
        region=region,
        profile="",
        access_key_last4=_mask_key(req.access_key_id),
        storage="secret_storage",
        message="AWS 자격증명이 유효합니다. VS Code 보안 금고에 저장할 수 있습니다.",
        permission_check=permission_check,
    )


def _known_profiles() -> list[str]:
    """~/.aws/credentials 와 ~/.aws/config 의 프로필 이름을 합친다.

    credentials 파일만 보면 SSO 프로필이 빠진다 — SSO 는 `[profile x]` 가
    config 에만 있고 credentials 에는 아무것도 없다. 프로필 연결의 대상은
    "boto3 가 해석할 수 있는 모든 프로필"이어야 한다.
    """
    names: list[str] = []
    if AWS_CREDENTIALS_FILE.exists():
        try:
            cp = configparser.RawConfigParser()
            cp.read(AWS_CREDENTIALS_FILE, encoding="utf-8")
            names.extend(cp.sections())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[aws] credentials parse failed: %s", exc)
    if AWS_CONFIG_FILE.exists():
        try:
            cfg = configparser.RawConfigParser()
            cfg.read(AWS_CONFIG_FILE, encoding="utf-8")
            for section in cfg.sections():
                names.append(section[len("profile "):] if section.startswith("profile ") else section)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[aws] config parse failed: %s", exc)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _apply_profile_to_process_env(profile: str, region: str) -> None:
    """프로필 연결용 환경 적용 — **키 환경변수를 지우는 것이 핵심이다.**

    boto3 는 AWS_ACCESS_KEY_ID 가 남아 있으면 AWS_PROFILE 보다 그걸
    우선한다. 이전에 키로 연결했던 흔적을 지우지 않으면, 사용자는 프로필
    A 를 골랐는데 요청은 옛 키 B 로 나간다 — 화면에는 A 로 연결됐다고
    뜨는 채로 **다른 계정에 배포되는** 조용한 사고다.
    """
    global _active_profile
    _active_profile = profile
    os.environ.pop("AWS_ACCESS_KEY_ID", None)
    os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
    os.environ.pop("AWS_SESSION_TOKEN", None)
    os.environ["AWS_PROFILE"] = profile
    if region:
        os.environ["AWS_DEFAULT_REGION"] = region
        os.environ["AWS_REGION"] = region


@router.post("/api/aws/connect-profile", response_model=AwsStatus)
async def connect_aws_profile(req: AwsProfileConnectRequest) -> AwsStatus:
    """~/.aws 프로필로 연결 — 키를 화면에 다시 입력받지 않는다.

    배경: 연결 수단이 "키 붙여넣기"뿐이라, AWS CLI 를 이미 쓰는 사용자도
    콘솔에서 키를 다시 찾아 복사해야 했다. 자격증명이 이미 이 컴퓨터에
    있는데 재입력을 강요하는 것은 마찰일 뿐 보안 이득이 없다.
    """
    profile = req.profile.strip()
    known = _known_profiles()
    if profile not in known:
        #: 없는 프로필을 boto3 에 넘기면 ProfileNotFound 가 원인 표시 없이
        #: 500 으로 떨어진다. 무엇이 가능한지까지 알려주며 400 으로 막는다.
        available = ", ".join(known) if known else "없음"
        raise HTTPException(
            status_code=400,
            detail=f"프로필 '{profile}' 을 ~/.aws 에서 찾지 못했습니다. 사용 가능한 프로필: {available}",
        )

    #: 리전이 비어 있으면 프로필 자신의 설정(config)에서 가져온다. 여기서
    #: 하드코딩 기본값으로 덮으면 키 연결 UI 가 예전에 겪은 리전 불일치
    #: 사고(사용자가 고른 적 없는 리전이 "현재 리전"이 되는 것)를 반복한다.
    effective_region = req.region.strip()
    if not effective_region:
        try:
            session = _build_boto3_session(profile=profile)
            effective_region = (session.region_name or "").strip()
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            effective_region = ""
    if not effective_region:
        effective_region = DEFAULT_REGION

    snapshot, prior_profile = _environment_snapshot()
    try:
        _apply_profile_to_process_env(profile, effective_region)
        identity = _call_sts_get_caller_identity(profile=profile, region=effective_region)
        permission_check = _inspect_deploy_permissions(
            identity, effective_region, req.deployment_context,
        )
    except Exception:
        _restore_environment(snapshot, prior_profile)
        raise

    _leave_role_mode()
    _refresh_diagnostics_cache()

    return AwsStatus(
        ready=True,
        identity=AwsIdentity(**identity),
        region=effective_region,
        profile=profile,
        access_key_last4="",
        storage="aws_profile",
        message=f"프로필 '{profile}' 로 연결되었습니다. 키는 ~/.aws 에 있는 것을 그대로 씁니다.",
        permission_check=permission_check,
    )


@router.get("/api/aws/status", response_model=AwsStatus)
async def get_aws_status() -> AwsStatus:
    """현재 AWS 자격증명 상태.

    자격증명이 없으면 ready=False 로 200 응답 (500 안 남).
    """
    _load_into_process_if_needed()
    _refresh_role_credentials()

    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )
    storage, profile = _detect_credential_source()

    if not access_key and not AWS_CREDENTIALS_FILE.exists():
        return AwsStatus(
            ready=False,
            identity=None,
            region=region,
            profile=profile,
            access_key_last4="",
            storage=storage,
            message="AWS 자격증명이 설정되지 않았습니다. /api/aws/configure 로 등록하세요.",
        )

    # boto3 미설치 → ready=False (500 아님)
    try:
        import boto3  # type: ignore  # noqa: F401
    except ImportError:
        return AwsStatus(
            ready=False,
            identity=None,
            region=region,
            profile=profile,
            access_key_last4=_mask_key(access_key),
            storage=storage,
            message="boto3 패키지가 설치되어 있지 않습니다.",
        )

    # STS 호출 — 역할 모드에서는 프로필(기반)이 아니라 환경변수(빌린
    # 임시 자격증명)를 검증해야 한다. 프로필로 부르면 기반 쪽이 살아 있는지만
    # 보고 "연결됨"이라 답하게 된다.
    try:
        identity = _call_sts_get_caller_identity(
            profile=None if storage == "assumed_role" else (profile or None),
            region=region,
        )
    except HTTPException as exc:
        return AwsStatus(
            ready=False,
            identity=None,
            region=region,
            profile=profile,
            access_key_last4=_mask_key(access_key),
            storage=storage,
            message=exc.detail if isinstance(exc.detail, str) else "AWS 검증 실패",
        )
    except Exception as exc:  # noqa: BLE001
        return AwsStatus(
            ready=False,
            identity=None,
            region=region,
            profile=profile,
            access_key_last4=_mask_key(access_key),
            storage=storage,
            message=f"AWS 검증 실패: {exc}",
        )

    return AwsStatus(
        ready=True,
        identity=AwsIdentity(**identity),
        region=region,
        profile=profile,
        access_key_last4=_mask_key(access_key),
        storage=storage,
        message=(
            "배포 전용 역할의 임시 자격증명으로 연결되어 있습니다."
            if storage == "assumed_role" else "AWS 자격증명이 유효합니다."
        ),
        **_role_status_fields(),
    )


def _role_status_fields() -> dict[str, str]:
    """AwsStatus 에 실을 역할 모드 필드 — 역할 모드가 아니면 빈 값."""
    state = _role_state
    if state is None:
        return {"role_arn": "", "expires_at": ""}
    expires = state.get("expires_at")
    return {
        "role_arn": str(state.get("role_arn") or ""),
        "expires_at": expires.isoformat() if isinstance(expires, datetime) else "",
    }


@router.post("/api/aws/role/setup", response_model=AwsRoleSetupResponse)
async def setup_aws_role(req: AwsRoleSetupRequest) -> AwsRoleSetupResponse:
    """프로그램 안에서 최소권한 역할을 만들고(있으면 맞추고) 빌려 연결한다.

    배경: quick-create 온보딩은 사용자를 콘솔로 보내고 키 두 개를 붙여넣게
    한다. 이 컴퓨터에 자격증명이 이미 있으면 그럴 필요가 없다 — 역할 하나를
    만들고 그 역할만 빌려 쓰면 콘솔도, 저장할 장기 키도 없다. 관리자급
    자격증명은 역할을 만드는 이 요청 안에서만 쓰고 저장하지 않는다.

    권한이 모자라면(iam:CreateRole 등) 200 + mode="console_fallback" 이다.
    호출자는 그때 /api/aws/onboarding-link 로 콘솔 경로를 연다.
    """
    global _role_state

    profile = req.profile.strip()
    if profile:
        known = _known_profiles()
        if profile not in known:
            available = ", ".join(known) if known else "없음"
            raise HTTPException(
                status_code=400,
                detail=f"프로필 '{profile}' 을 ~/.aws 에서 찾지 못했습니다. 사용 가능한 프로필: {available}",
            )
        base_profile = profile
        base_env = None
    else:
        #: 지금 연결된 자격증명이 기반이다. 이미 역할 모드면 그 역할의 기반을
        #: 그대로 쓴다(역할의 임시 자격증명으로는 역할을 못 만든다).
        if _role_state is not None:
            base_profile = str(_role_state.get("base_profile") or "")
            base_env = _role_state.get("base_env")
        else:
            base_profile = (os.environ.get("AWS_PROFILE") or _active_profile or "").strip()
            base_env = None if base_profile else _base_credentials_from_env()
            if not base_profile and not base_env:
                #: 명시적으로 연결한 적이 없어도 ~/.aws 의 기본 프로필로 "연결됨" 상태일 수
                #: 있다(status 가 그렇게 판정한다). 그 상태에서 "배포 전용 역할로 전환" 을
                #: 누르면 기반이 없다고 400 이 났다(2026-09-21 실기기). status 와 같은
                #: 기준으로 기반을 정한다.
                _storage, detected = _detect_credential_source()
                if detected and detected in _known_profiles():
                    base_profile = detected
        if not base_profile and not base_env:
            raise HTTPException(
                status_code=400,
                detail="역할을 만들 기반 자격증명이 없습니다. 프로필을 고르거나 키로 먼저 연결하세요.",
            )

    region = req.region.strip()
    if not region and base_profile:
        try:
            region = (_build_boto3_session(profile=base_profile).region_name or "").strip()
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            region = ""
    if not region:
        region = (
            os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION
        )
    region = aws_policy.validate_region(region)

    session = _base_session(base_profile, base_env, region)

    # 1) 누가 만드는가 — 이 주체만 역할을 빌릴 수 있게 신뢰 정책을 좁힌다.
    try:
        identity = session.client("sts", region_name=region).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"기반 자격증명 검증 실패: {exc}") from exc
    account = str(identity.get("Account", ""))
    caller_arn = str(identity.get("Arn", ""))
    partition = _simulation_partition(caller_arn)
    principal_arn = aws_role.principal_for_trust(caller_arn, account, partition)

    # 2) 역할 만들기/맞추기 + 3) 정책 붙이기 + 4) 빌리기
    ctx, _ctx_error = _resolved_permission_context(req.deployment_context)
    snapshot, prior_profile = _environment_snapshot()
    try:
        result = aws_role.ensure_deploy_role(
            session.client("iam", region_name=region),
            account=account, region=region, principal_arn=principal_arn, partition=partition,
            task_execution_role=(ctx.task_execution_role if ctx else ""),
            task_role=(ctx.task_role if ctx else ""),
            cluster=(ctx.ecs_cluster if ctx else ""),
            service=(ctx.ecs_service if ctx else ""),
            ecr_repo=(ctx.ecr_repo if ctx else ""),
        )
        creds = aws_role.assume_deploy_role(
            session.client("sts", region_name=region), result.role_arn,
        )
    except aws_role.RoleSetupDenied as denied:
        _restore_environment(snapshot, prior_profile)
        return AwsRoleSetupResponse(
            ok=False,
            mode="console_fallback",
            denied_action=denied.action,
            message=(
                f"이 자격증명에는 {denied.action} 권한이 없어 프로그램 안에서 역할을 만들 수 없습니다. "
                "콘솔에서 만드는 경로(원클릭 IAM 셋업)로 안내합니다."
            ),
        )
    except ValueError as exc:
        _restore_environment(snapshot, prior_profile)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        _restore_environment(snapshot, prior_profile)
        raise HTTPException(status_code=500, detail=f"역할 설정 실패: {exc}") from exc

    # 5) 코어를 역할의 임시 자격증명으로 바꾼다
    _apply_role_credentials(creds, region)
    _role_state = {
        "role_arn": result.role_arn,
        "base_profile": base_profile,
        "base_env": base_env,
        "region": region,
        "expires_at": creds.expiration,
        "principal_arn": principal_arn,
    }
    os.environ[ENV_ASSUME_ROLE_ARN] = result.role_arn

    # 6) 빌린 자격증명으로 검증·권한 점검 — 화면은 "무엇으로 연결됐나"를 본다
    try:
        role_identity = _call_sts_get_caller_identity(profile=None, region=region)
        permission_check = _inspect_deploy_permissions(role_identity, region, req.deployment_context)
    except HTTPException as exc:
        _leave_role_mode()
        _restore_environment(snapshot, prior_profile)
        raise HTTPException(
            status_code=500,
            detail=f"역할은 만들었지만 빌린 자격증명 검증에 실패했습니다: {exc.detail}",
        ) from exc

    _refresh_diagnostics_cache()

    what = "만들었습니다" if result.created else ("신뢰 정책을 갱신했습니다" if result.trust_updated else "확인했습니다")
    status = AwsStatus(
        ready=True,
        identity=AwsIdentity(**role_identity),
        region=region,
        profile=base_profile,
        access_key_last4="",
        storage="assumed_role",
        message=f"배포 전용 역할 {result.role_name} 을(를) {what}. 이제 그 역할의 임시 자격증명만 씁니다.",
        permission_check=permission_check,
        **_role_status_fields(),
    )
    return AwsRoleSetupResponse(
        ok=True,
        mode="role",
        message=status.message,
        role=aws_role.role_summary(result, creds),
        status=status,
    )


@router.post("/api/aws/role/refresh", response_model=AwsStatus)
async def refresh_aws_role() -> AwsStatus:
    """역할 모드의 임시 자격증명을 지금 다시 빌린다 (만료 임박 여부와 무관)."""
    if _role_state is None:
        raise HTTPException(status_code=400, detail="역할 모드가 아닙니다.")
    _role_state["expires_at"] = datetime.now(timezone.utc)  # 강제로 만료 임박 취급
    _refresh_role_credentials()
    return await get_aws_status()


@router.post("/api/aws/permissions/check", response_model=AwsStatus)
async def check_aws_permissions(
    req: Optional[AwsPermissionCheckRequest] = None,
) -> AwsStatus:
    """저장·재입력 없이 현재 연결된 자격증명의 배포 권한을 다시 점검한다."""
    status = await get_aws_status()
    if not status.ready or status.identity is None:
        return status
    identity = {
        "account": status.identity.account,
        "arn": status.identity.arn,
        "user_id": status.identity.user_id,
    }
    status.permission_check = _inspect_deploy_permissions(
        identity,
        status.region or DEFAULT_REGION,
        req.deployment_context if req else None,
    )
    return status


@router.post("/api/aws/configure", response_model=AwsStatus)
async def configure_aws(req: AwsConfigureRequest) -> AwsStatus:
    """AWS 자격증명 저장 + 즉시 STS 검증 + diagnostics 캐시 갱신.

    실패 시:
    - 잘못된 키 → 401
    - 권한 부족 → 403
    - boto3 미설치 → 503
    - 그 외 → 500
    파일 저장은 검증 성공 후에만 일어난다.
    """
    region = (req.region or DEFAULT_REGION).strip()
    profile = (req.profile or "recoder").strip()
    storage = (req.storage or "recoder").strip()

    if storage not in ("recoder", "aws_credentials_file"):
        raise HTTPException(
            status_code=400,
            detail="storage 는 'recoder' 또는 'aws_credentials_file' 이어야 합니다.",
        )

    # 1) 우선 프로세스 환경변수에 임시 적용 (다음 boto3 세션이 이 값을 사용)
    _apply_env_snapshot = {
        "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID"),
        "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY"),
        "AWS_SESSION_TOKEN": os.environ.get("AWS_SESSION_TOKEN"),
        "AWS_REGION": os.environ.get("AWS_REGION"),
        "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION"),
        "AWS_PROFILE": os.environ.get("AWS_PROFILE"),
    }

    _apply_to_process_env(
        access_key_id=req.access_key_id,
        secret_access_key=req.secret_access_key,
        region=region,
        profile=profile,
        session_token=req.session_token,
    )

    # 2) STS 검증 — profile 인자 없이 환경변수만으로 호출
    try:
        identity = _call_sts_get_caller_identity(profile=None, region=region)
    except HTTPException:
        # 검증 실패 → 환경변수 롤백 후 그대로 에러 전파
        for key, val in _apply_env_snapshot.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        raise
    except Exception as exc:  # noqa: BLE001
        for key, val in _apply_env_snapshot.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        raise HTTPException(status_code=500, detail=f"STS 검증 실패: {exc}") from exc

    # 3) 검증 통과 → 디스크 저장
    try:
        if storage == "aws_credentials_file":
            _save_aws_credentials_file(
                req.access_key_id,
                req.secret_access_key,
                region,
                profile,
                req.session_token,
            )
        else:
            _save_recoder_credentials(
                req.access_key_id,
                req.secret_access_key,
                region,
                profile,
                req.session_token,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[aws] credentials save failed")
        raise HTTPException(status_code=500, detail=f"자격증명 저장 실패: {exc}") from exc

    # 4) diagnostics 캐시 무효화/재실행
    _leave_role_mode()
    _refresh_diagnostics_cache()

    return AwsStatus(
        ready=True,
        identity=AwsIdentity(**identity),
        region=region,
        profile=profile,
        access_key_last4=_mask_key(req.access_key_id),
        storage=storage,
        message="AWS 자격증명이 저장되고 검증되었습니다.",
    )


@router.post("/api/aws/clear")
async def clear_aws() -> dict[str, Any]:
    """저장된 AWS 자격증명 제거 (~/.recoder/aws_credentials.json).

    ~/.aws/credentials 의 [profile] 섹션은 사용자 안전을 위해 자동 제거하지 않는다.
    환경변수는 현재 프로세스 한정으로 unset 한다.
    """
    removed_path: Optional[str] = None
    if CREDENTIALS_FILE.exists():
        try:
            CREDENTIALS_FILE.unlink()
            removed_path = str(CREDENTIALS_FILE)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500,
                detail=f"자격증명 파일 삭제 실패: {exc}",
            ) from exc

    # 환경변수 unset (이 프로세스만)
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
    ):
        os.environ.pop(key, None)

    global _active_profile
    _active_profile = None
    #: 역할 모드도 끝낸다. IAM 에 만든 역할 자체는 지우지 않는다 — 다음
    #: 온보딩에서 그대로 다시 쓰고, 지우는 건 사용자가 콘솔에서 판단한다.
    _leave_role_mode()

    _refresh_diagnostics_cache()

    return {
        "status": "ok",
        "removed_path": removed_path,
        "message": "AWS 자격증명이 제거되었습니다." if removed_path
                   else "삭제할 자격증명 파일이 없습니다.",
    }


@router.get("/api/aws/profiles")
async def list_aws_profiles() -> dict[str, list[str]]:
    """사용 가능한 profile 목록 — credentials 와 config(SSO 포함)를 합친다.

    credentials 파일만 읽으면 SSO 프로필이 목록에서 빠져서, 사용자는
    "aws cli 로는 되는데 여기엔 안 뜨는" 상태를 겪는다. connect-profile
    이 받아 주는 것과 정확히 같은 집합을 보여줘야 한다.
    """
    return {"profiles": _known_profiles()}


@router.get("/api/aws/ecr/repos")
async def list_ecr_repos(region: str = "", profile: str = "", max_results: int = 50) -> dict[str, Any]:
    """ECR 레포지토리 목록 — 자격증명 sanity-check 겸용.

    Query:
      region: 지정 시 해당 리전에서 조회. 미지정 시 환경변수 기본값.
      profile: 지정 시 해당 profile 사용.
      max_results: 1~1000.
    """
    _load_into_process_if_needed()

    # 권한표 쪽과 **같은 방식**으로 리전을 정한다. 예전에는 여기만 하드코딩
    # 기본값(`ap-northeast-2`)으로 떨어져서, 같은 세션인데 두 엔드포인트가
    # 서로 다른 리전을 말했다. 사용자는 `us-west-2` 에 있는데 "리포지토리가
    # 없습니다"를 보게 된다.
    resolved_profile = profile.strip() or _effective_profile()
    _, session_region, _ = _deployment_identity()
    resolved_region = (
        region.strip()
        or session_region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )

    session = _build_boto3_session(profile=resolved_profile, region=resolved_region)
    try:
        ecr = session.client("ecr", region_name=resolved_region)
        kwargs: dict[str, Any] = {"maxResults": max(1, min(int(max_results), 1000))}
        resp = ecr.describe_repositories(**kwargs)
        repos = [
            {
                "name": r.get("repositoryName", ""),
                "uri": r.get("repositoryUri", ""),
                "arn": r.get("repositoryArn", ""),
                "created_at": r.get("createdAt").isoformat()
                if r.get("createdAt") else "",
                "image_tag_mutability": r.get("imageTagMutability", ""),
            }
            for r in resp.get("repositories", [])
        ]
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "InvalidClientTokenId" in msg or "SignatureDoesNotMatch" in msg:
            raise HTTPException(status_code=401, detail=f"AWS 자격증명 무효: {msg}") from exc
        if "AccessDenied" in msg or "NotAuthorized" in msg:
            # 이건 고장이 아니라 **의도된 결과**다.
            #
            # 이 엔드포인트는 리포지토리 이름을 안 주고 계정 전체를 훑는다.
            # 그런데 최소권한 정책은 ECR 조회를 `recoder-*` 로 좁혀 놓는다
            # (사용자의 다른 리포지토리 이름까지 보여줄 이유가 없다).
            # 그래서 권한표를 정확히 따른 사용자일수록 여기서 막힌다.
            #
            # 403 으로 끊으면 화면이 "자격증명이 잘못됐다"로 표시해 사용자가
            # 멀쩡한 키를 의심하게 된다. 목록만 비우고 이유를 알린다.
            logger.info("[aws] ECR 계정 전체 목록 거부 — 최소권한 정책에서는 정상: %s", msg)
            return {
                "region": resolved_region,
                "profile": resolved_profile or "",
                "repositories": [],
                "listing_denied": True,
                "message": (
                    "최소권한 정책에서는 계정 전체 ECR 목록 조회를 허용하지 않습니다. "
                    "권한표대로 설정하셨다면 정상이며, 자격증명 문제가 아닙니다. "
                    "연결 확인은 /api/aws/status 를 쓰세요."
                ),
            }
        if "Unable to locate credentials" in msg:
            raise HTTPException(
                status_code=400,
                detail="AWS 자격증명이 설정되어 있지 않습니다.",
            ) from exc
        raise HTTPException(status_code=500, detail=f"ECR 조회 실패: {msg}") from exc

    return {
        "region": resolved_region,
        "profile": resolved_profile or "",
        "repositories": repos,
        "listing_denied": False,
        "message": "",
    }


# ---------------------------------------------------------------------------
# 최소권한 권한표 (FR-04-02 · ADR-D10/D12)
# ---------------------------------------------------------------------------
#
# 사용자가 키를 만들기 **전에** 부르는 엔드포인트다. 자격증명이 없어도
# 반드시 200 으로 응답해야 한다 — 권한을 몰라서 키를 못 만드는 상황을
# 없애는 것이 이 기능의 목적이기 때문이다.


class AwsPolicyResponse(BaseModel):
    """복사해 붙일 정책 + 따라 할 순서."""
    policy: dict
    policy_json: str          # 콘솔에 그대로 붙여넣을 문자열
    targets: list[str]
    action_count: int
    needs_manual_fill: bool   # 계정/리전 자리표시자가 남아 있는가
    account_id: str = ""
    region: str = ""
    task_execution_role: str = ""   # ECS 작업이 쓸 실행 역할 이름
    task_role: str = ""             # 컨테이너 안 코드가 쓸 역할 (실행 역할과 다름)
    cluster: str = ""               # 정책이 허용한 ECS 클러스터 이름
    service: str = ""               # 정책이 허용한 ECS 서비스 이름
    ecr_repo: str = ""              # 정책이 허용한 ECR 리포지토리 이름
    is_academy_account: bool = False  # 학교(AWS Academy) 러너랩 세션인가
    steps: list[str] = []


def _policy_steps(unknowns: list[str], academy: bool = False) -> list[str]:
    """콘솔에서 따라 할 순서.

    학교(AWS Academy) 계정은 **IAM 사용자를 만들 수 없다.** 실제 러너랩
    계정에서 확인했다 — `iam:CreateUser` 가 허용되지 않는다. 그런 계정에
    "사용자를 만드세요"라고 안내하면 3단계에서 막히고, 사용자는 자기가 뭘
    잘못한 줄 안다. 그래서 안내 자체를 갈라 놓는다.
    """
    if academy:
        return [
            "학교(AWS Academy) 계정은 IAM 사용자·정책을 만들 수 없습니다. "
            "아래 정책은 참고용이고, 실제로는 랩이 주는 임시 자격증명을 그대로 씁니다",
            "러너랩 화면 → AWS Details → AWS CLI → 3줄(액세스 키·비밀 키·세션 토큰) 복사",
            # 화면으로 안내하면 안 된다. 'AWS 연결' 화면에는 세션 토큰 입력칸이
            # 없어서(FR-04-01 미비) 3번째 줄이 들어갈 데가 없다. 넣어봐야
            "그 3줄을 '~/.aws/credentials' 파일에 직접 넣으세요. "
            "지금 'AWS 연결' 화면에는 세션 토큰 입력칸이 없어 학교 계정 "
            "자격증명을 넣을 수 없습니다",
            "region = us-east-1 도 같은 파일에 적으세요. 학교 계정은 이 리전만 됩니다",
            "랩 세션이 끝나면 자격증명이 만료됩니다. 만료되면 그 3줄을 다시 덮어쓰세요",
            f"ECS 작업의 실행 역할로는 미리 만들어져 있는 "
            f"'{aws_policy.ACADEMY_TASK_EXECUTION_ROLE}' 을 씁니다 "
            f"(학교 계정에는 '{aws_policy.TASK_EXECUTION_ROLE}' 이 없습니다)",
        ]

    steps = [
        "AWS 콘솔 → IAM → 정책(Policies) → 정책 생성 → JSON 탭",
        "아래 정책을 붙여넣고 이름을 'ReCoderMinimal' 로 저장",
    ]
    if unknowns:
        what = " · ".join(unknowns)
        hint = f"정책 안의 {what} 를 본인 값으로 바꾸기"
        if aws_policy.PARTITION_PLACEHOLDER in unknowns:
            # 파티션은 낯선 개념이라 값까지 알려준다. 안 그러면 여기서 막힌다.
            hint += (
                f" — {aws_policy.PARTITION_PLACEHOLDER} 는 대부분 'aws' 이고, "
                f"미국 정부용(GovCloud)이면 'aws-us-gov', 중국이면 'aws-cn' 입니다"
            )
        steps.insert(2, hint)
    steps += [
        "IAM → 사용자 → 사용자 생성 → 방금 만든 정책 연결",
        "해당 사용자에서 액세스 키 발급 (용도: 로컬 코드)",
        "발급된 액세스 키를 ReCoder 의 'AWS 연결' 화면에 입력",
    ]
    return steps


#: 학교 계정임을 알아보는 표시. 러너랩은 `voclabs` 역할로 로그인시킨다.
ACADEMY_ARN_MARKERS = ("assumed-role/voclabs", ":role/LabRole")


def _looks_like_academy(arn: str) -> bool:
    """호출자가 AWS Academy 러너랩 세션인가.

    맞으면 IAM 사용자 생성 안내를 보여줘 봐야 막히기만 한다.
    """
    return any(marker in (arn or "") for marker in ACADEMY_ARN_MARKERS)


def _shared_profile_names() -> set[str]:
    """`~/.aws/credentials` · `~/.aws/config` 에 **실제로 존재하는** 프로필 이름."""
    names: set[str] = set()
    for path, prefix in ((AWS_CREDENTIALS_FILE, ""), (AWS_CONFIG_FILE, "profile ")):
        if not path.exists():
            continue
        cp = configparser.RawConfigParser()
        try:
            cp.read(path, encoding="utf-8")
        except Exception:  # pragma: no cover - 깨진 파일이 권한표를 막지 않는다
            continue
        for section in cp.sections():
            names.add(section[len(prefix):] if prefix and section.startswith(prefix)
                      else section)
    return names


def _effective_profile() -> Optional[str]:
    """배포가 **실제로** 쓸 프로필. 없거나 못 쓰면 None(=boto3 기본 체인).

    두 가지를 거른다.

    **① 자격증명이 환경변수에 이미 주입돼 있으면 프로필을 넘기지 않는다.**
    ReCoder 기본 저장 방식(`storage="recoder"`)은 키를 `~/.recoder/` 에 두고
    환경변수로 주입하면서 `AWS_PROFILE` 에 `"recoder"` 라는 **라벨**을 같이
    심는다. 그런데 그 이름의 공유 프로필은 존재하지 않는다. 그대로 세션에
    넘기면 `ProfileNotFound` 로 죽고, 멀쩡한 자격증명이 연결돼 있는데도
    권한표가 자리표시자만 담아 나간다.

    **② 이름이 있어도 공유 프로필로 존재할 때만 쓴다.** 없는 이름을 넘기는
    것보다 boto3 기본 체인에 맡기는 편이 항상 낫다.

    `AWS_PROFILE` 자체는 계속 존중한다 — 배포 클라이언트가 그걸 보기 때문에,
    무시하면 정책과 배포가 다른 계정을 가리킨다.
    """
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        return None
    name = (os.environ.get("AWS_PROFILE") or "").strip() or _active_profile
    if not name:
        return None
    return name if name in _shared_profile_names() else None


def _deployment_identity() -> tuple[str, str, str]:
    """(계정, 리전, 호출자 ARN). 모르는 값은 빈 문자열.

    **세 값을 한 세션에서 뽑는다.** 계정은 `get_aws_status()` 에서, 리전은
    다른 경로에서 가져오면 서로 다른 프로필을 가리킬 수 있고, 그러면 정책이
    "A 계정의 B 리전" 같은 존재하지 않는 조합을 그린다.

    리전은 세션에서 가져온다 — 프로필의 `~/.aws/config` 까지 읽는 유일한
    경로다. 하드코딩 기본값으로는 절대 떨어지지 않는다. 모르면 비워서
    자리표시자가 남게 한다. **틀린 값을 채우는 것보다 낫다.**
    """
    _load_into_process_if_needed()
    profile = _effective_profile()
    session = None
    for attempt in ([profile] if profile else []) + [None]:
        try:
            session = _build_boto3_session(profile=attempt, region="")
            break
        except Exception:
            # 프로필로 실패하면 프로필 **없이** 한 번 더 시도한다.
            # 자격증명이 환경변수에 있는데 이름 하나 때문에 통째로 포기하면,
            # 멀쩡한 사용자가 자리표시자만 받는다.
            logger.info("[aws] 프로필 %r 로 세션 실패 — 기본 체인으로 재시도", attempt)
    if session is None:  # pragma: no cover - boto3 자체가 없는 경우
        return "", "", ""

    region = (getattr(session, "region_name", "") or "").strip()
    account, caller_arn = "", ""
    try:
        identity = session.client("sts", region_name=region or None).get_caller_identity()
        account = identity.get("Account", "") or ""
        caller_arn = identity.get("Arn", "") or ""
    except Exception:  # 자격증명이 없거나 막혀도 권한표는 나와야 한다
        pass
    return account, region, caller_arn


def _resolve_roles(
    academy: bool, task_execution_role: str, task_role: str
) -> tuple[str, str]:
    """역할 이름 결정 — **`aws_policy.resolve_roles()` 에 그대로 위임한다.**

    여기에 갈래를 하나라도 더 두면 안 된다. 예전에 여기서 학교 계정을
    `LabRole` 로 바꿨는데, 배포 경로는 환경변수만 보므로 **정책은 LabRole,
    배포는 ecsTaskExecutionRole** 이 되어 학교 계정 사용자가 안내대로 해도
    배포 전 점검에서 실패했다.

    학교 계정은 `_academy_role_advice()` 로 **안내**만 한다.
    """
    return aws_policy.resolve_roles(task_execution_role, task_role)


def _task_role_note(task_role: str) -> list[str]:
    """태스크 역할이 없을 때 **왜 없는지** 알려준다.

    권한표에서 항목 하나가 사라지면 사용자는 빠뜨린 줄 안다. 없는 게
    맞다는 걸 말해 줘야 한다 — 그리고 앱이 AWS API 를 부르는 경우에는
    어떻게 넣는지도.
    """
    if task_role:
        return []
    return [
        f"태스크 역할(taskRoleArn)은 이 권한표에 들어 있지 않습니다. "
        f"컨테이너 안의 앱이 AWS API 를 직접 부르지 않으면 필요 없고, "
        f"안 쓰는 역할에 iam:PassRole 을 주면 그만큼 권한이 넓어집니다. "
        f"필요하면 {aws_policy.ENV_TASK_ROLE_ARN} 환경변수를 설정한 뒤 "
        f"권한표를 다시 받으세요 — 그때 자동으로 포함됩니다"
    ]


def _academy_role_advice(academy: bool) -> list[str]:
    """학교 계정인데 역할 환경변수가 안 잡혀 있으면 알려준다.

    자동으로 바꿔주지 않는 이유는 `aws_policy.resolve_roles()` 참고 —
    배포 경로가 안 따라오면 정책만 바뀌어 봐야 갈라질 뿐이다.
    """
    if not academy:
        return []
    lab = aws_policy.ACADEMY_TASK_EXECUTION_ROLE
    # **실행 역할만 보면 된다.** 태스크 역할은 안 정하는 게 정상이다
    # (`task_role_if_configured()`). 태스크 역할까지 요구하면, 실행 역할을
    # 제대로 맞춰 둔 사람에게 "태스크 역할도 설정하세요"라고 잔소리해 놓고
    # 바로 다음 줄에서 "태스크 역할은 이 권한표에 없고 필요 없습니다"라고
    # 말하게 된다 — 사용자는 어느 쪽을 믿어야 할지 알 수 없다.
    if aws_policy.configured_execution_role() == lab:
        return []
    return [
        f"⚠ 학교(AWS Academy) 계정으로 보입니다. 이 계정에는 "
        f"'{aws_policy.TASK_EXECUTION_ROLE}' 이 없고 '{lab}' 만 있습니다. "
        f"아래 환경변수를 설정한 뒤 이 권한표를 다시 받으세요 — "
        f"설정해야 정책과 실제 배포가 같은 역할을 봅니다:\n"
        f"    {aws_policy.ENV_EXECUTION_ROLE_ARN}=arn:aws:iam::<계정ID>:role/{lab}\n"
        f"  (컨테이너 안 앱이 AWS API 를 부른다면 "
        f"{aws_policy.ENV_TASK_ROLE_ARN} 도 같은 값으로 설정하세요. "
        f"안 부르면 설정하지 않는 편이 권한이 좁습니다.)"
    ]


@router.get("/api/aws/policy", response_model=AwsPolicyResponse)
async def get_minimum_policy(
    targets: str = "",
    task_execution_role: str = "",
    task_role: str = "",
    cluster: str = "",
    service: str = "",
    ecr_repo: str = "",
    region: str = "",
) -> AwsPolicyResponse:
    """ReCoder 가 요구하는 최소권한 IAM 정책을 돌려준다.

    `targets` 는 쉼표로 구분한다 (`ecs,s3,bedrock`). 비우면 전체.
    이미 자격증명이 연결돼 있으면 계정 ID·리전을 채워 돌려주고,
    없으면 자리표시자를 남긴 뒤 `needs_manual_fill=True` 로 알린다.

    ## 이름 인자들

    `task_execution_role` / `task_role` 은 ECS 작업에 붙는 역할 **이름**이다.
    둘은 서로 다른 역할이고, 태스크 역할까지 쓰는 배포라면
    `RegisterTaskDefinition` 이 둘 다에 대해 `iam:PassRole` 을 요구한다.

    **`task_role` 은 비우는 것이 기본이다.** 배포 경로는
    `ECS_TASK_ROLE_ARN` 이 설정됐을 때만 태스크 역할을 붙이므로, 안 붙이는
    배포에 그 역할 권한을 넣으면 쓰지도 않는 권한만 넓어진다.

    학교(AWS Academy) 계정이라도 **역할을 자동으로 바꾸지 않는다.** 배포
    경로가 환경변수만 보기 때문에, 정책만 `LabRole` 로 바꾸면 둘이 갈라진다.
    대신 `_academy_role_advice()` 로 환경변수를 설정하라고 안내한다.

    `cluster` / `service` 는 배포 대상 이름이다. 비우면 우리가 만드는 자원의
    기본 규칙(`recoder-*`)을 쓴다. **이미 있는 클러스터(`default` 등)에
    배포한다면 반드시 넘겨야 한다** — 안 그러면 정책을 그대로 붙여도 배포 전
    점검에서 막힌다.

    이름이 잘못되면(특히 와일드카드) 400 으로 거부한다. `role/*` 짜리 정책을
    뽑아낼 수 있으면 이 기능의 존재 이유가 사라진다.
    """
    selected = [t.strip() for t in targets.split(",") if t.strip()] or None
    # 계정·리전·호출자를 **한 세션에서** 뽑는다. 섞으면 서로 다른 프로필을
    # 가리키는 조합이 만들어진다.
    account_id, session_region, caller_arn = _deployment_identity()
    explicit_region = (region or "").strip()
    resolved_region = explicit_region or session_region
    if resolved_region and not explicit_region:
        # 세션에서 끌어온 값은 **사용자가 타이핑한 게 아니다.** 우리가 모르는
        # 리전 이름(새 리전, 주권 클라우드 등)이면 "당신 리전이 잘못됐다"고
        # 400 을 던질 게 아니라, 자리표시자로 내려서 권한표는 주고 사용자가
        # 채우게 한다. 직접 지정한 값이 틀렸을 때만 400 이 맞다.
        try:
            aws_policy.validate_region(resolved_region)
        except ValueError:
            logger.info("[aws] 세션 리전 형식을 모르겠음 — 자리표시자로 둔다: %r",
                        resolved_region)
            resolved_region = ""

    academy = _looks_like_academy(caller_arn)
    try:
        exec_role, task = _resolve_roles(academy, task_execution_role, task_role)
    except ValueError as exc:
        # 역할 환경변수가 잘못된 경우도 여기로 온다. 500 이 아니라 400 이어야
        # 사용자가 "내 설정이 잘못됐구나"를 안다.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    names = {
        "task_role": task,
        "cluster": (cluster or "").strip() or aws_policy.DEFAULT_CLUSTER,
        "service": (service or "").strip() or aws_policy.DEFAULT_SERVICE,
        "ecr_repo": (ecr_repo or "").strip() or aws_policy.DEFAULT_ECR_REPO,
    }

    try:
        policy = aws_policy.build_policy(
            selected, account_id, resolved_region, exec_role, **names
        )
        policy_text = aws_policy.policy_json(
            selected, account_id, resolved_region, exec_role, **names
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    unknowns = aws_policy.placeholders_in(policy)
    needs_fill = bool(unknowns)
    return AwsPolicyResponse(
        policy=policy,
        policy_json=policy_text,
        targets=list(selected or aws_policy.DEFAULT_TARGETS),
        action_count=len(aws_policy.used_actions(policy)),
        needs_manual_fill=needs_fill,
        account_id=account_id,
        region=resolved_region,
        task_execution_role=exec_role,
        task_role=task,
        cluster=names["cluster"],
        service=names["service"],
        ecr_repo=names["ecr_repo"],
        is_academy_account=academy,
        steps=_academy_role_advice(academy)
        + _task_role_note(task)
        + _policy_steps(unknowns, academy),
    )


# ---------------------------------------------------------------------------
# 온보딩 원클릭 IAM 셋업 — 보드 카드 「AWS 온보딩 마찰 제거」
# ---------------------------------------------------------------------------

class AwsOnboardingResponse(BaseModel):
    """quick-create 링크 + 템플릿 본문 + 따라 할 순서."""
    quick_create_url: str        # 템플릿이 호스팅돼 있을 때만 채워진다
    template_hosted: bool
    console_upload_url: str      # 폴백 — 콘솔의 템플릿 업로드 화면
    template_body: str           # CloudFormation 템플릿(JSON) 원문
    stack_name: str
    action_count: int            # 정책이 허용하는 액션 수 (최소권한 근거)
    steps: list[str]


@router.get("/api/aws/onboarding-link", response_model=AwsOnboardingResponse)
async def get_onboarding_link(region: str = "", targets: str = "") -> AwsOnboardingResponse:
    """원클릭 IAM 셋업 링크를 만든다.

    템플릿은 정적 하나다 — 계정 ID·리전은 CloudFormation 내장 변수가 스택
    생성 시점에 채우므로 사용자별 생성이 필요 없다. 템플릿이 S3 에 호스팅돼
    있으면(코어 .env 의 RECODER_IAM_TEMPLATE_URL) quick-create 링크를 주고,
    아니면 콘솔 업로드 플로우로 폴백한다. 확장은 템플릿 본문을 클립보드에
    복사해 두므로 폴백에서도 붙여넣기 한 번이면 된다.
    """
    try:
        import aws_onboarding
    except ImportError:  # 패키지 상대 배치 폴백 (aws_policy 와 동일 패턴)
        from core import aws_onboarding  # type: ignore

    selected = [t.strip() for t in targets.split(",") if t.strip()] or None
    explicit_region = (region or "").strip()
    if explicit_region:
        try:
            aws_policy.validate_region(explicit_region)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        # 이미 연결돼 있으면 그 리전의 콘솔을 연다. 없으면 리전 없이 —
        # 콘솔이 사용자의 마지막 리전을 쓴다.
        _, session_region, _ = _deployment_identity()
        explicit_region = session_region or ""

    try:
        template_body = aws_onboarding.template_json(targets=selected)
        template = aws_onboarding.build_quickcreate_template(selected)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    policy_doc = template["Resources"]["RecoderDeployPolicy"]["Properties"]["PolicyDocument"]
    hosted = aws_onboarding.hosted_template_url()

    if hosted:
        steps = [
            "1. 열린 브라우저에서 스택 이름을 확인하고 「스택 생성」을 누르세요.",
            "2. 생성이 끝나면 Outputs 탭에서 AccessKeyId / SecretAccessKey 를 복사하세요.",
            "3. ReCoder 의 AWS 연결 화면에 붙여넣으면 끝입니다.",
        ]
    else:
        steps = [
            "1. 템플릿이 클립보드에 복사됐습니다. 열린 콘솔에서 「템플릿 파일 업로드」를 고르고, 붙여넣어 저장한 파일을 올리세요.",
            "2. 스택 이름은 recoder-iam-setup 을 권장합니다. 「스택 생성」을 누르세요.",
            "3. Outputs 탭에서 AccessKeyId / SecretAccessKey 를 복사해 ReCoder 의 AWS 연결 화면에 붙여넣으세요.",
            f"(팀 참고: 템플릿을 S3 에 올리고 {aws_onboarding.ENV_TEMPLATE_URL} 을 설정하면 이 과정이 링크 클릭 한 번으로 줄어듭니다 — infra/README.md)",
        ]

    return AwsOnboardingResponse(
        quick_create_url=aws_onboarding.quick_create_url(hosted, explicit_region),
        template_hosted=bool(hosted),
        console_upload_url=aws_onboarding.console_upload_url(explicit_region),
        template_body=template_body,
        stack_name=aws_onboarding.STACK_NAME,
        action_count=len(aws_policy.used_actions(_strip_cfn_subs(policy_doc))),
        steps=steps,
    )


def _strip_cfn_subs(value):
    """Fn::Sub 래핑을 벗겨 일반 정책 문서 모양으로 되돌린다 (액션 계수용)."""
    if isinstance(value, dict):
        if set(value.keys()) == {"Fn::Sub"}:
            return value["Fn::Sub"]
        return {k: _strip_cfn_subs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_cfn_subs(v) for v in value]
    return value
