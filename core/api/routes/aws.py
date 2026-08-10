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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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


class AwsIdentity(BaseModel):
    account: str = ""
    arn: str = ""
    user_id: str = ""


class AwsStatus(BaseModel):
    ready: bool = False
    identity: Optional[AwsIdentity] = None
    region: str = ""
    profile: str = ""
    access_key_last4: str = ""
    storage: str = ""        # "recoder" | "aws_credentials_file" | "env" | ""
    message: str = ""


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
    storage_label: "recoder" | "aws_credentials_file" | "env" | ""
    """
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
        return
    stored = _load_stored_credentials()
    if not stored:
        return
    _apply_to_process_env(
        access_key_id=stored.get("access_key_id", ""),
        secret_access_key=stored.get("secret_access_key", ""),
        region=stored.get("region", DEFAULT_REGION),
        profile=stored.get("profile", "recoder"),
        session_token=stored.get("session_token", ""),
    )


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
    except Exception:
        _restore_environment(snapshot, prior_profile)
        raise

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
    )


@router.get("/api/aws/status", response_model=AwsStatus)
async def get_aws_status() -> AwsStatus:
    """현재 AWS 자격증명 상태.

    자격증명이 없으면 ready=False 로 200 응답 (500 안 남).
    """
    _load_into_process_if_needed()

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

    # STS 호출
    try:
        identity = _call_sts_get_caller_identity(
            profile=profile or None,
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
        message="AWS 자격증명이 유효합니다.",
    )


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

    _refresh_diagnostics_cache()

    return {
        "status": "ok",
        "removed_path": removed_path,
        "message": "AWS 자격증명이 제거되었습니다." if removed_path
                   else "삭제할 자격증명 파일이 없습니다.",
    }


@router.get("/api/aws/profiles")
async def list_aws_profiles() -> dict[str, list[str]]:
    """~/.aws/credentials 의 사용 가능한 profile 목록."""
    profiles: list[str] = []
    if AWS_CREDENTIALS_FILE.exists():
        try:
            cp = configparser.RawConfigParser()
            cp.read(AWS_CREDENTIALS_FILE, encoding="utf-8")
            profiles = list(cp.sections())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[aws] profiles parse failed: %s", exc)

    return {"profiles": profiles}


@router.get("/api/aws/ecr/repos")
async def list_ecr_repos(region: str = "", profile: str = "", max_results: int = 50) -> dict[str, Any]:
    """ECR 레포지토리 목록 — 자격증명 sanity-check 겸용.

    Query:
      region: 지정 시 해당 리전에서 조회. 미지정 시 환경변수 기본값.
      profile: 지정 시 해당 profile 사용.
      max_results: 1~1000.
    """
    _load_into_process_if_needed()

    resolved_region = (
        region.strip()
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )
    resolved_profile = profile.strip() or _active_profile or None

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
            raise HTTPException(
                status_code=403,
                detail=f"ECR DescribeRepositories 권한 거부: {msg}",
            ) from exc
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
    }
