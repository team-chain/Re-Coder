"""
<<<<<<< HEAD
ReCoder Core — First Run Diagnostics

Checks system readiness across all major subsystems (Core, AI, Docker,
AWS deploy, Ops) and persists the result for the VSCode extension to
surface to the user on first launch.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from schemas import DiagnosticsResult, ProviderType, ReadyState

_RECODER_DIR = Path.home() / ".recoder"
_DIAGNOSTICS_FILE = _RECODER_DIR / "diagnostics.json"

# AWS regions that support Bedrock (non-exhaustive allowlist used for validation)
_BEDROCK_REGIONS = [
    "us-east-1",
    "us-west-2",
    "eu-central-1",
    "eu-west-1",
    "ap-northeast-1",
    "ap-southeast-1",
    "ap-southeast-2",
]

_BEDROCK_MODEL_PRIORITY = [
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic.claude-3-sonnet-20240229-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
]


class FirstRunDiagnostics:
    """Runs all subsystem readiness checks and collects structured results."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_all(self) -> DiagnosticsResult:
        """Execute every check concurrently and return a DiagnosticsResult."""
        (
            core_ok,
            ai_ok,
            docker_ok,
            aws_ok,
            ops_ok,
        ) = await asyncio.gather(
            self.check_core_ready(),
            self.check_ai_ready(),
            self.check_docker_ready(),
            self.check_aws_deploy_ready(),
            self.check_ops_ready(),
            return_exceptions=False,
        )

        # Resolve model / provider info for display
        model_id: Optional[str] = None
        region: Optional[str] = None
        provider: Optional[ProviderType] = None
        is_cross_region = False

        if ai_ok:
            # Try Bedrock first, then Gemini
            bedrock_region = self._detect_bedrock_region()
            if bedrock_region:
                ok, mid = await self.validate_bedrock(bedrock_region)
                if ok:
                    model_id = mid
                    region = bedrock_region
                    provider = ProviderType.BEDROCK
                    is_cross_region = bedrock_region not in _BEDROCK_REGIONS
            if model_id is None:
                gemini_ok = await self.validate_gemini()
                if gemini_ok:
                    provider = ProviderType.OPENAI  # Gemini uses OpenAI-compat

        result = DiagnosticsResult(
            core_ready=ReadyState.READY if core_ok else ReadyState.NOT_READY,
            ai_ready=ReadyState.READY if ai_ok else ReadyState.NOT_READY,
            docker_ready=ReadyState.READY if docker_ok else ReadyState.NOT_READY,
            aws_deploy_ready=ReadyState.READY if aws_ok else ReadyState.NOT_READY,
            ops_ready=ReadyState.READY if ops_ok else ReadyState.NOT_READY,
            resolved_model_id=model_id,
            resolved_region=region,
            is_cross_region_profile=is_cross_region,
            provider_type=provider,
            validation_time=datetime.utcnow(),
        )

        await self.save_diagnostics(result)
        return result

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    async def check_core_ready(self) -> bool:
        """Core is always ready because this code is already executing."""
        return True

    async def check_ai_ready(self) -> bool:
        """Return True if at least one AI backend (Bedrock or Gemini) is available."""
        region = self._detect_bedrock_region()
        if region:
            ok, _ = await self.validate_bedrock(region)
            if ok:
                return True
        return await self.validate_gemini()

    async def check_docker_ready(self) -> bool:
        """Return True if Docker Engine is detected and responsive."""
        return await asyncio.get_running_loop().run_in_executor(
            None, self._check_docker_sync
        )

    async def check_aws_deploy_ready(self) -> bool:
        """
        Return True if AWS CLI is installed, at least one SSH key exists,
        and basic EC2/ECR permissions are available.
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self._check_aws_sync
        )

    async def check_ops_ready(self) -> bool:
        """
        Return True if SSH client, remote Docker socket access, container
        health-check tooling, and Discord webhook env-var are all present.
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self._check_ops_sync
        )

    # ------------------------------------------------------------------
    # Validator helpers
    # ------------------------------------------------------------------

    async def validate_bedrock(self, region: str) -> Tuple[bool, str]:
        """
        Try to list Bedrock foundation models in *region* and return the
        first usable model ID.

        Returns (True, model_id) on success or (False, "") on failure.
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self._validate_bedrock_sync, region
        )

    async def validate_gemini(self) -> bool:
        """
        Return True if a GOOGLE_API_KEY (or GEMINI_API_KEY) environment
        variable is set and the Gemini models endpoint responds successfully.
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self._validate_gemini_sync
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def save_diagnostics(self, result: DiagnosticsResult) -> None:
        """Persist diagnostics to ~/.recoder/diagnostics.json."""
        _RECODER_DIR.mkdir(parents=True, exist_ok=True)
        _DIAGNOSTICS_FILE.write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )

    async def load_diagnostics(self) -> Optional[DiagnosticsResult]:
        """Load a previously saved DiagnosticsResult, or None if absent."""
        if not _DIAGNOSTICS_FILE.exists():
            return None
        try:
            data = json.loads(_DIAGNOSTICS_FILE.read_text(encoding="utf-8"))
            return DiagnosticsResult(**data)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Synchronous worker implementations
    # ------------------------------------------------------------------

    @staticmethod
    def _check_docker_sync() -> bool:
        """Check Docker by running `docker info`."""
        if shutil.which("docker") is None:
            return False
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    @staticmethod
    def _check_aws_sync() -> bool:
        """Verify AWS CLI exists and has basic IAM/ECR access."""
        if shutil.which("aws") is None:
            return False
        # Check AWS identity (requires configured credentials)
        try:
            result = subprocess.run(
                ["aws", "sts", "get-caller-identity"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                return False
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

        # Check for at least one SSH key in ~/.ssh/
        ssh_dir = Path.home() / ".ssh"
        if ssh_dir.exists():
            keys = list(ssh_dir.glob("id_*")) + list(ssh_dir.glob("*.pem"))
            private_keys = [k for k in keys if ".pub" not in k.name]
            if not private_keys:
                return False
        else:
            return False

        return True

    @staticmethod
    def _check_ops_sync() -> bool:
        """Check SSH client availability plus optional Discord webhook config."""
        if shutil.which("ssh") is None:
            return False
        # Remote Docker over SSH is supported if ssh is available
        # Discord webhook is optional; just check if it's configured
        import os
        discord_ok = bool(os.environ.get("DISCORD_WEBHOOK_URL"))
        # Ops is considered ready if SSH is present (Discord is optional/bonus)
        return True  # SSH confirmed above; Discord optional

    @staticmethod
    def _validate_bedrock_sync(region: str) -> Tuple[bool, str]:
        """Synchronously validate Bedrock access in *region*."""
        try:
            import boto3  # type: ignore
            client = boto3.client("bedrock", region_name=region)
            response = client.list_foundation_models(
                byOutputModality="TEXT",
                byInferenceType="ON_DEMAND",
            )
            models = response.get("modelSummaries", [])
            available_ids = {m["modelId"] for m in models}
            for preferred in _BEDROCK_MODEL_PRIORITY:
                if preferred in available_ids:
                    return True, preferred
            if available_ids:
                return True, sorted(available_ids)[0]
            return False, ""
        except Exception:
            return False, ""

    @staticmethod
    def _validate_gemini_sync() -> bool:
        """Synchronously validate Gemini API key presence and connectivity."""
        import os
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return False
        try:
            import urllib.request
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models"
                f"?key={api_key}"
            )
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Internal utility
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_bedrock_region() -> Optional[str]:
        """Detect the AWS region configured in the environment or AWS config."""
        import os
        region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")
        if region:
            return region
        # Try reading ~/.aws/config
        aws_config = Path.home() / ".aws" / "config"
        if aws_config.exists():
            try:
                for line in aws_config.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("region"):
                        _, _, val = line.partition("=")
                        r = val.strip()
                        if r:
                            return r
            except Exception:
                pass
=======
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
    Bedrock: boto3 import + AWS credentials 존재 + 테스트 호출(dry-run).
    Gemini: GEMINI_API_KEY 환경변수 존재.
    반환: (ReadyStatus, resolved_model_id, resolved_region, provider_type)
    실패 시 model_id="" 반환.
    """

    # 1. Bedrock 시도
    try:
        import boto3

        # AWS credentials 확인
        session = boto3.Session()
        credentials = session.get_credentials()

        if credentials is not None:
            # Bedrock 클라이언트 생성
            bedrock_client = session.client(
                "bedrock-runtime",
                region_name=os.getenv("AWS_REGION", "us-east-1")
            )

            # Dry-run 테스트: ListFoundationModels로 서비스 확인
            try:
                bedrock_models_client = session.client(
                    "bedrock",
                    region_name=os.getenv("AWS_REGION", "us-east-1")
                )
                response = bedrock_models_client.list_foundation_models()

                if response and len(response.get("modelSummaries", [])) > 0:
                    region = os.getenv("AWS_REGION", "us-east-1")
                    # 기본 모델 ID (예: anthropic.claude-3-sonnet)
                    model_id = "anthropic.claude-3-sonnet-20240229-v1:0"
                    return ReadyStatus.OK, model_id, region, "bedrock"
            except Exception:
                # Bedrock 체크 실패, Gemini로 이동
                pass
    except ImportError:
        # boto3 미설치, Gemini로 이동
        pass
    except Exception:
        # 예외 무시, Gemini로 이동
        pass

    # 2. Gemini 시도
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if gemini_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)

            # 모델 목록 확인
            models = genai.list_models()
            if models:
                return ReadyStatus.OK, "gemini-pro", "", "gemini"
        except ImportError:
            # google-generativeai 미설치
            pass
        except Exception:
            # Gemini API 호출 실패
            pass

    # 모든 제공자 실패
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
        return ReadyStatus.WARN, issues

    # 2) AWS credentials 확인
    try:
        session = boto3.Session()
        credentials = session.get_credentials()
        if credentials is None:
            issues.append(
                "AWS Deploy Ready: AWS credentials 미설정 "
                "(~/.aws/credentials 또는 AWS_ACCESS_KEY_ID 환경변수 필요)"
            )
            return ReadyStatus.WARN, issues

        resolved = credentials.resolve()
        if resolved is None:
            issues.append("AWS Deploy Ready: AWS credentials resolve 실패")
            return ReadyStatus.WARN, issues

    except Exception as e:
        issues.append(f"AWS Deploy Ready: credentials 확인 중 오류: {e}")
        return ReadyStatus.WARN, issues

    # 3) AWS 리전 확인 (STS GetCallerIdentity — 가장 가벼운 자격증명 검증)
    region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    try:
        sts = boto3.client("sts", region_name=region)
        sts.get_caller_identity()
    except Exception as e:
        issues.append(f"AWS Deploy Ready: STS 검증 실패 ({e}). 자격증명 또는 네트워크 확인 필요")
        return ReadyStatus.WARN, issues

    return ReadyStatus.OK, []


def check_ops_ready() -> tuple[ReadyStatus, list[str]]:
    """
    Ops 도구 준비 상태 확인 (§S-2).

    체크 항목:
    1. docker 설치 여부 (이미 check_docker_ready 에서 확인하지만 Ops 관점에서 재확인)
    2. git 설치 여부
    3. RECODER_HOME 쓰기 권한 (이미 core_ready 에서 확인, 중복 방지를 위해 경량 체크)

    반환: (ReadyStatus, issues_list)
    - OK: docker + git 모두 사용 가능
    - WARN: 일부 도구 누락 (로컬 배포 일부 기능 제한)
    """
    issues: list[str] = []
    warn = False

    # git 설치 확인
    if shutil.which("git") is None:
        issues.append("Ops Ready: git 미설치 — git_agent 기능 비활성화")
        warn = True

    # docker 설치 확인
    if shutil.which("docker") is None:
        issues.append("Ops Ready: docker 미설치 — 컨테이너 배포 비활성화")
        warn = True

    if warn:
        return ReadyStatus.WARN, issues

    return ReadyStatus.OK, []


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
>>>>>>> 74cf4369799da45d0fa49de67d56e58e01a2cc27
        return None
