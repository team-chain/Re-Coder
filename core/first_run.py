"""
First Run 진단 마법사 (설계서 v6.4 §11)
5단계 Ready 상태 진단 + diagnostics.json 저장.
Core Ready → AI Ready → Docker Ready → (2학기: AWS/Ops Ready)
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from schemas import DiagnosticsResult, ReadyStatus

RECODER_HOME = Path(os.getenv("RECODER_HOME", str(Path.home() / ".recoder")))
DIAGNOSTICS_PATH = RECODER_HOME / "diagnostics.json"


async def run_diagnostics() -> DiagnosticsResult:
    """
    전체 진단 실행. 결과를 diagnostics.json에 저장.
    순서: Core Ready → AI Ready → Docker Ready
    """
    result = DiagnosticsResult()

    # Step 1: Core Ready 체크
    core_status, core_issues = check_core_ready()
    result.core_ready = core_status
    result.issues.extend(core_issues)

    # Step 2: AI Ready 체크
    ai_status, model_id, region, provider = await check_ai_ready()
    result.ai_ready = ai_status
    result.resolved_model_id = model_id
    result.resolved_region = region
    result.provider_type = provider
    if model_id == "":
        result.issues.append("AI Ready: No provider available (Bedrock or Gemini)")

    # Step 3: Docker Ready 체크
    docker_status, docker_version = check_docker_ready()
    result.docker_ready = docker_status
    result.docker_version = docker_version
    if docker_status == ReadyStatus.FAIL:
        result.issues.append("Docker Ready: Docker Engine not found")

    # Step 4: AWS Deploy Ready (§S-2)
    aws_status, aws_issues = check_aws_deploy_ready()
    result.aws_deploy_ready = aws_status
    result.issues.extend(aws_issues)

    # Step 5: Ops Ready (§S-2)
    ops_status, ops_issues = check_ops_ready()
    result.ops_ready = ops_status
    result.issues.extend(ops_issues)

    # 검증 시간 기록
    result.validation_time = datetime.now(timezone.utc).isoformat()

    # 결과 저장
    save_diagnostics(result)

    return result


def check_core_ready() -> tuple[ReadyStatus, list[str]]:
    """
    ~/.recoder/ 디렉터리 존재 + 쓰기 권한 확인.
    runtime.json 생성 가능 여부.
    Windows ACL Soft Fail (§11.3): 권한 설정 실패 시 경고만 남기고 OK 반환.
    반환: (ReadyStatus, issues_list)
    """
    issues = []

    # 디렉터리 존재 확인
    if not RECODER_HOME.exists():
        try:
            RECODER_HOME.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            issues.append(f"Core Ready: Failed to create RECODER_HOME: {e}")
            return ReadyStatus.FAIL, issues

    # 쓰기 권한 확인
    try:
        test_file = RECODER_HOME / ".write_test"
        test_file.write_text("test")
        test_file.unlink()
    except Exception as e:
        issues.append(f"Core Ready: No write permission in RECODER_HOME: {e}")
        return ReadyStatus.FAIL, issues

    # runtime.json 생성 가능 여부 확인
    runtime_path = RECODER_HOME / "runtime.json"
    try:
        runtime_data = {
            "version": "6.4",
            "check_time": datetime.now(timezone.utc).isoformat()
        }
        runtime_path.write_text(json.dumps(runtime_data, indent=2))
    except Exception as e:
        issues.append(f"Core Ready: Failed to create runtime.json: {e}")
        return ReadyStatus.FAIL, issues

    # Windows ACL 설정 시도 (Soft Fail)
    if sys.platform == "win32":
        try:
            os.system(f'icacls "{RECODER_HOME}" /inheritance:d /grant:r "%USERNAME%:F" >nul 2>&1')
        except Exception:
            # Soft Fail: 권한 설정 실패 시에도 OK 반환
            pass
    else:
        # macOS/Linux: chmod 0700
        try:
            RECODER_HOME.chmod(0o700)
        except Exception:
            pass

    return ReadyStatus.OK, []


async def check_ai_ready() -> tuple[ReadyStatus, str, str, str]:
    """
    Bedrock 또는 Gemini 중 최소 1개 가용 확인.
    Bedrock: boto3 의 기본 자격증명 체인을 통해 검증
             (env / shared credentials / shared config / SSO / IAM Identity Center 등 모두 포함).
    Gemini: GEMINI_API_KEY 환경변수 존재.
    반환: (ReadyStatus, resolved_model_id, resolved_region, provider_type)
    실패 시 model_id="" 반환.
    """

    bedrock_region = os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION", "us-east-1")

    # ── Bedrock 검사 ────────────────────────────────────────────────
    # boto3 의 기본 credential resolver 를 사용 → SSO / IAM Identity Center /
    # ~/.aws/config 의 profile / WebIdentityToken / IMDS 등 모든 표준 경로를
    # 한꺼번에 인식한다. (기존 로직은 환경변수 + ~/.aws/credentials 파일만
    # 검사해 SSO 사용자에게는 항상 FAIL 로 판정되는 문제가 있었음.)
    try:
        import boto3  # type: ignore

        try:
            session = boto3.Session()
            credentials = session.get_credentials()
            if credentials is not None:
                frozen = credentials.get_frozen_credentials()
                if frozen and frozen.access_key:
                    model_id = (
                        os.getenv("BEDROCK_FAST_MODEL_IDENTIFIER")
                        or "anthropic.claude-3-haiku-20240307-v1:0"
                    )
                    return ReadyStatus.OK, model_id, bedrock_region, "bedrock"
        except Exception:
            # Bedrock 자격증명 resolve 중 예외 → Gemini 폴백으로 넘어감
            pass
    except ImportError:
        pass

    # ── Gemini 폴백 ────────────────────────────────────────────────
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        gemini_model = (
            os.getenv("GEMINI_MODEL", "").strip()
            or "gemini-2.5-flash"
        )
        return ReadyStatus.OK, gemini_model, "", "gemini"

    return ReadyStatus.FAIL, "", "", ""


def check_docker_ready() -> tuple[ReadyStatus, str]:
    """
    docker --version 실행으로 Docker Engine 감지.
    반환: (ReadyStatus, docker_version_string)
    """
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            version_string = result.stdout.strip()
            return ReadyStatus.OK, version_string
    except FileNotFoundError:
        # docker 명령어를 찾을 수 없음
        pass
    except subprocess.TimeoutExpired:
        # 타임아웃
        pass
    except Exception:
        # 기타 예외
        pass

    return ReadyStatus.FAIL, ""


def check_aws_deploy_ready() -> tuple[ReadyStatus, list[str]]:
    """
    AWS 배포 준비 상태 확인 (§S-2).

    체크 항목:
    1. boto3 패키지 설치 여부
    2. AWS credentials 존재 여부 (~/.aws/credentials 또는 환경변수)
    3. ECR/ECS 기본 접근 가능 여부 (soft check)

    반환: (ReadyStatus, issues_list)
    - OK: boto3 있고 credentials 유효
    - WARN: boto3 없거나 credentials 미설정 (배포는 불가, 로컬 모드로 fallback)
    """
    issues: list[str] = []

    # 1) boto3 설치 확인
    try:
        import boto3  # type: ignore
    except ImportError:
        issues.append("AWS Deploy Ready: boto3 미설치 (pip install boto3)")
        return ReadyStatus.PARTIAL, issues

    # 2) AWS credentials 확인
    try:
        session = boto3.Session()
        credentials = session.get_credentials()
        if credentials is None:
            issues.append(
                "AWS Deploy Ready: AWS credentials 미설정 "
                "(~/.aws/credentials 또는 AWS_ACCESS_KEY_ID 환경변수 필요)"
            )
            return ReadyStatus.PARTIAL, issues

        frozen = credentials.get_frozen_credentials()
        if frozen is None or not frozen.access_key:
            issues.append("AWS Deploy Ready: AWS credentials resolve 실패")
            return ReadyStatus.PARTIAL, issues

    except Exception as e:
        issues.append(f"AWS Deploy Ready: credentials 확인 중 오류: {e}")
        return ReadyStatus.PARTIAL, issues

    # 3) AWS 리전 확인 (STS GetCallerIdentity — 가장 가벼운 자격증명 검증)
    region = os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    try:
        sts = boto3.client("sts", region_name=region)
        sts.get_caller_identity()
    except Exception as e:
        issues.append(f"AWS Deploy Ready: STS 검증 실패 ({e}). 자격증명 또는 네트워크 확인 필요")
        return ReadyStatus.PARTIAL, issues

    return ReadyStatus.OK, []


def check_ops_ready() -> tuple[ReadyStatus, list[str]]:
    """
    Ops 도구 준비 상태 확인 (§S-2).

    1학기: FAIL 고정 반환 — 2학기에 실제 구현 예정.
    반환: (ReadyStatus.FAIL, ["2학기 구현 예정"])
    """
    return ReadyStatus.FAIL, ["2학기 구현 예정"]


def setup_recoder_home() -> None:
    """
    ~/.recoder/ 하위 디렉터리 생성.
    디렉터리: sessions/, backups/, logs/, projects/, templates/
    macOS/Linux: chmod 0700
    Windows: ACL 설정 시도, 실패 시 경고만 (Soft Fail)
    """
    RECODER_HOME.mkdir(parents=True, exist_ok=True)

    subdirs = [
        "sessions",
        "backups",
        "logs",
        "projects",
        "templates"
    ]

    for subdir in subdirs:
        subdir_path = RECODER_HOME / subdir
        subdir_path.mkdir(parents=True, exist_ok=True)

        # 권한 설정
        if sys.platform == "win32":
            # Windows: ACL 설정 시도 (Soft Fail)
            try:
                os.system(f'icacls "{subdir_path}" /inheritance:d /grant:r "%USERNAME%:F" >nul 2>&1')
            except Exception:
                pass
        else:
            # macOS/Linux: chmod 0700
            try:
                subdir_path.chmod(0o700)
            except Exception:
                pass


def save_diagnostics(result: DiagnosticsResult) -> None:
    """결과를 ~/.recoder/diagnostics.json에 저장"""
    RECODER_HOME.mkdir(parents=True, exist_ok=True)

    with open(DIAGNOSTICS_PATH, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2)


def load_diagnostics() -> DiagnosticsResult | None:
    """저장된 diagnostics.json 로드. 없으면 None."""
    if not DIAGNOSTICS_PATH.exists():
        return None

    try:
        with open(DIAGNOSTICS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Dictionary에서 DiagnosticsResult로 변환
        return DiagnosticsResult(
            core_ready=ReadyStatus(data.get("core_ready", "fail")),
            ai_ready=ReadyStatus(data.get("ai_ready", "fail")),
            docker_ready=ReadyStatus(data.get("docker_ready", "fail")),
            aws_deploy_ready=ReadyStatus(data.get("aws_deploy_ready", "fail")),
            ops_ready=ReadyStatus(data.get("ops_ready", "fail")),
            resolved_model_id=data.get("resolved_model_id", ""),
            resolved_region=data.get("resolved_region", ""),
            provider_type=data.get("provider_type", ""),
            is_cross_region_profile=data.get("is_cross_region_profile", False),
            validation_time=data.get("validation_time", ""),
            docker_version=data.get("docker_version", ""),
            issues=data.get("issues", [])
        )
    except Exception:
        return None
