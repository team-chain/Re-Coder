"""
ReCoder Core — First Run Diagnostics (설계서 v6.4 §11)

5단계 Ready 상태 진단 + diagnostics.json 저장.
Core Ready → AI Ready → Docker Ready → AWS Deploy Ready → Ops Ready

결과는 ~/.recoder/diagnostics.json에 저장되어 VSCode 확장이 첫 실행
시 사용자에게 진단 결과를 표시할 수 있도록 한다.

포함 필드: resolved_model_id, resolved_region, is_cross_region_profile,
provider_type, validation_time.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from schemas import DiagnosticsResult, ReadyStatus

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

RECODER_HOME = Path(os.getenv("RECODER_HOME", str(Path.home() / ".recoder")))
DIAGNOSTICS_PATH = RECODER_HOME / "diagnostics.json"

# Backwards-compatible aliases (used by older modules)
_RECODER_DIR = RECODER_HOME
_DIAGNOSTICS_FILE = DIAGNOSTICS_PATH

# AWS regions that support Bedrock (non-exhaustive allowlist used to flag
# cross-region inference profiles).
_BEDROCK_REGIONS = [
    "us-east-1",
    "us-west-2",
    "eu-central-1",
    "eu-west-1",
    "ap-northeast-1",
    "ap-southeast-1",
    "ap-southeast-2",
]

# Preferred Bedrock models in priority order — Claude 4.x 우선, 없으면 3.x 폴백.
_BEDROCK_MODEL_PRIORITY = [
    "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-sonnet-4-20250514-v1:0",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic.claude-3-sonnet-20240229-v1:0",
    "anthropic.claude-3-5-haiku-20241022-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
]


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

async def run_diagnostics() -> DiagnosticsResult:
    """
    전체 진단 실행. 결과를 diagnostics.json에 저장.
    순서: Core Ready → AI Ready → Docker Ready → AWS Deploy Ready → Ops Ready
    """
    result = DiagnosticsResult()

    # Step 1: Core Ready
    core_status, core_issues = check_core_ready()
    result.core_ready = core_status
    result.issues.extend(core_issues)

    # Step 2: AI Ready
    ai_status, model_id, region, provider, is_cross_region = await check_ai_ready()
    result.ai_ready = ai_status
    result.resolved_model_id = model_id
    result.resolved_region = region
    result.provider_type = provider
    result.is_cross_region_profile = is_cross_region
    if not model_id:
        result.issues.append("AI Ready: No provider available (Bedrock or Gemini)")

    # Step 3: Docker Ready
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


# ---------------------------------------------------------------------------
# Individual readiness checks
# ---------------------------------------------------------------------------

def check_core_ready() -> tuple[ReadyStatus, list[str]]:
    """
    ~/.recoder/ 디렉터리 존재 + 쓰기 권한 확인.
    runtime.json 생성 가능 여부.
    Windows ACL Soft Fail (§11.3): 권한 설정 실패 시 경고만 남기고 OK 반환.

    반환: (ReadyStatus, issues_list)
    """
    issues: list[str] = []

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

    # 쓰기 가능 여부 확인 — 별도 파일 사용.
    # 주의: 절대 "runtime.json" 을 쓰면 안 된다. 그 파일은 singleton 이 port/session_token/pid
    # 를 저장하는 곳이라, 여기서 덮어쓰면 토큰이 날아가 인증(401) 이 깨진다. (실제 버그였음)
    runtime_path = RECODER_HOME / "ready_check.json"
    try:
        runtime_data = {
            "version": "6.4",
            "check_time": datetime.now(timezone.utc).isoformat(),
        }
        runtime_path.write_text(json.dumps(runtime_data, indent=2))
    except Exception as e:
        issues.append(f"Core Ready: Failed to create ready_check.json: {e}")
        return ReadyStatus.FAIL, issues

    # 권한 설정 (Soft Fail)
    if sys.platform == "win32":
        try:
            os.system(
                f'icacls "{RECODER_HOME}" /inheritance:d '
                f'/grant:r "%USERNAME%:F" >nul 2>&1'
            )
        except Exception:
            pass
    else:
        try:
            RECODER_HOME.chmod(0o700)
        except Exception:
            pass

    return ReadyStatus.OK, []


async def check_ai_ready() -> tuple[ReadyStatus, str, str, str, bool]:
    """
    Strict 검증: 실제 invoke (Bedrock converse 또는 Gemini list_models) 가
    성공해야만 OK. 자격증명만 있고 호출이 실패하면 FAIL.

    Bedrock 검증 순서:
        1) ListFoundationModels + on-demand 매칭 모델 발견 → 그 모델로 converse ping
        2) .env BEDROCK_PRIMARY_MODEL_IDENTIFIER 직접 converse ping
        3) Cross-region inference profile 후보 (apac./us./eu. prefix) 순회하며 ping
        4) 위 SONNET_MODELS/HAIKU_MODELS 체인 순회 ping
        하나라도 성공하면 OK, 모두 실패하면 다음 단계(Gemini)로.

    Gemini 검증:
        - GEMINI_API_KEY/GOOGLE_API_KEY 존재 + list_models 호출 성공.

    반환: (ReadyStatus, model_id, region, provider_type, is_cross_region_profile)
    실패 시 빈 문자열들과 ReadyStatus.FAIL.
    """

    import logging
    log = logging.getLogger(__name__)

    # 1. Bedrock 시도
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    detected = _detect_bedrock_region()
    if detected:
        region = detected

    def _converse_ping(runtime_client, model_id: str) -> bool:
        """1-token converse ping. 성공 시 True, 실패 시 False."""
        try:
            runtime_client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": "ping"}]}],
                inferenceConfig={"maxTokens": 1},
            )
            return True
        except Exception as exc:
            log.debug("Bedrock ping failed model=%s: %s", model_id, exc)
            return False

    try:
        import boto3  # type: ignore

        session = boto3.Session()
        credentials = session.get_credentials()

        if credentials is not None:
            primary_model = (
                os.getenv("BEDROCK_PRIMARY_MODEL_IDENTIFIER")
                or os.getenv("BEDROCK_FAST_MODEL_IDENTIFIER")
                or os.getenv("BEDROCK_SECONDARY_MODEL_IDENTIFIER")
                or ""
            ).strip()

            is_cross_region = region not in _BEDROCK_REGIONS
            runtime = session.client("bedrock-runtime", region_name=region)

            # ── 1차: ListFoundationModels → on-demand 모델 발견 시 converse ping ──
            try:
                bedrock_models_client = session.client("bedrock", region_name=region)
                response = bedrock_models_client.list_foundation_models(
                    byOutputModality="TEXT",
                    byInferenceType="ON_DEMAND",
                )
                models = response.get("modelSummaries", []) if response else []
                available_ids = {m["modelId"] for m in models}
                for preferred in _BEDROCK_MODEL_PRIORITY:
                    if preferred in available_ids and _converse_ping(runtime, preferred):
                        return ReadyStatus.OK, preferred, region, "bedrock", is_cross_region
                # 사전 우선순위 매칭 안 되면 발견된 모델 중 첫 번째로 ping
                for mid in sorted(available_ids):
                    if _converse_ping(runtime, mid):
                        return ReadyStatus.OK, mid, region, "bedrock", is_cross_region
            except Exception as list_exc:
                log.debug("ListFoundationModels 실패: %s — invoke fallback 진행", list_exc)

            # ── 2차: .env primary_model 직접 converse ping ──
            if primary_model and _converse_ping(runtime, primary_model):
                return ReadyStatus.OK, primary_model, region, "bedrock", is_cross_region

            # ── 3차: cross-region inference profile 후보 (apac./us./eu. prefix) ──
            # primary_model 이 prefix 없는 raw model id 면 region 매핑 prefix 시도.
            if primary_model and not primary_model.startswith(("us.", "apac.", "eu.")):
                region_prefix_map = {
                    "ap-northeast-1": "apac.", "ap-northeast-2": "apac.",
                    "ap-northeast-3": "apac.", "ap-southeast-1": "apac.",
                    "ap-southeast-2": "apac.", "ap-south-1": "apac.",
                    "us-east-1": "us.", "us-east-2": "us.",
                    "us-west-1": "us.", "us-west-2": "us.",
                    "eu-central-1": "eu.", "eu-west-1": "eu.",
                    "eu-west-2": "eu.", "eu-west-3": "eu.",
                    "eu-north-1": "eu.",
                }
                prefix = region_prefix_map.get(region, "")
                if prefix:
                    candidate = prefix + primary_model
                    if _converse_ping(runtime, candidate):
                        return ReadyStatus.OK, candidate, region, "bedrock", True

            # ── 4차: BedrockProvider 의 SONNET/HAIKU 체인 순회 ──
            try:
                from llm.bedrock_provider import SONNET_MODELS, HAIKU_MODELS  # type: ignore
                fallback_chain = list(dict.fromkeys(SONNET_MODELS + HAIKU_MODELS))
            except Exception:
                fallback_chain = [
                    "anthropic.claude-3-haiku-20240307-v1:0",
                    "anthropic.claude-3-sonnet-20240229-v1:0",
                    "anthropic.claude-3-5-haiku-20241022-v1:0",
                    "anthropic.claude-3-5-sonnet-20241022-v2:0",
                    "apac.anthropic.claude-sonnet-4-5-20250929-v1:0",
                    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                ]
            for mid in fallback_chain:
                if mid == primary_model:
                    continue  # 이미 시도
                if _converse_ping(runtime, mid):
                    is_xreg = mid.startswith(("us.", "apac.", "eu."))
                    return ReadyStatus.OK, mid, region, "bedrock", is_xreg

            log.debug("Bedrock 모든 invoke ping 실패 — Gemini fallback 진행")
    except ImportError:
        pass
    except Exception as exc:
        log.debug("Bedrock 진단 전체 실패: %s", exc)

    # 2. Gemini 시도
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
    if gemini_key:
        # google-generativeai SDK 우선 시도
        try:
            import google.generativeai as genai  # type: ignore

            genai.configure(api_key=gemini_key)
            models = genai.list_models()
            if models:
                return ReadyStatus.OK, "gemini-pro", "", "gemini", False
        except ImportError:
            pass
        except Exception:
            pass

        # SDK 실패 시 HTTPS 직접 호출로 fallback
        try:
            import urllib.request

            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models"
                f"?key={gemini_key}"
            )
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return ReadyStatus.OK, "gemini-pro", "", "gemini", False
        except Exception:
            pass

    # 모든 제공자 실패
    return ReadyStatus.FAIL, "", "", "", False


def check_docker_ready() -> tuple[ReadyStatus, str]:
    """
    Strict: docker CLI 존재 + docker --version 성공 + docker info 응답(daemon up)
    셋 다 만족할 때만 OK. 하나라도 실패하면 FAIL.

    이전엔 daemon down 일 때 PARTIAL 을 반환했으나, 실제 `docker build` 가
    즉시 실패하므로 ✓ 표시는 거짓 양성이다. Docker Desktop 을 켜야 ✓.

    반환: (ReadyStatus, docker_version_string)
    """
    if shutil.which("docker") is None:
        return ReadyStatus.FAIL, ""

    try:
        version_result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if version_result.returncode != 0:
            return ReadyStatus.FAIL, ""

        version_string = version_result.stdout.strip()

        # docker info — daemon 이 응답해야만 OK. 안 되면 FAIL (거짓 양성 금지).
        try:
            info_result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=8,
            )
            if info_result.returncode != 0:
                return ReadyStatus.FAIL, version_string  # daemon down
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ReadyStatus.FAIL, version_string

        return ReadyStatus.OK, version_string
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass

    return ReadyStatus.FAIL, ""


def check_aws_deploy_ready() -> tuple[ReadyStatus, list[str]]:
    """
    AWS 배포 준비 상태 확인 (§S-2).

    체크 항목:
    1. boto3 패키지 설치 여부
    2. AWS credentials 존재 여부 (~/.aws/credentials 또는 환경변수)
    3. STS GetCallerIdentity 호출로 자격증명 검증
    4. AWS CLI 존재 + SSH 키 (EC2 배포용) 확인 (정보성)

    반환: (ReadyStatus, issues_list)
    - OK: boto3 있고 credentials 유효 + STS 검증 통과
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

        resolved = credentials.get_frozen_credentials() \
            if hasattr(credentials, "get_frozen_credentials") else None
        if resolved is None and hasattr(credentials, "resolve"):
            try:
                resolved = credentials.resolve()
            except Exception:
                resolved = None
        # resolved가 없어도 STS 호출로 최종 검증되므로 hard-fail 하지는 않음.
    except Exception as e:
        issues.append(f"AWS Deploy Ready: credentials 확인 중 오류: {e}")
        return ReadyStatus.WARN, issues

    # 3) STS GetCallerIdentity — 자격증명 최종 검증
    region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    try:
        sts = boto3.client("sts", region_name=region)
        sts.get_caller_identity()
    except Exception as e:
        issues.append(
            f"AWS Deploy Ready: STS 검증 실패 ({e}). 자격증명 또는 네트워크 확인 필요"
        )
        return ReadyStatus.WARN, issues

    # 4) 정보성 체크 — AWS CLI 및 SSH 키 (실패해도 WARN으로만 처리)
    if shutil.which("aws") is None:
        issues.append("AWS Deploy Ready: aws CLI 미설치 (일부 기능 제한 가능)")

    ssh_dir = Path.home() / ".ssh"
    if ssh_dir.exists():
        keys = list(ssh_dir.glob("id_*")) + list(ssh_dir.glob("*.pem"))
        private_keys = [k for k in keys if ".pub" not in k.name]
        if not private_keys:
            issues.append("AWS Deploy Ready: SSH 사설키 미발견 (EC2 SSH 배포 제한)")
    else:
        issues.append("AWS Deploy Ready: ~/.ssh 디렉터리 없음 (EC2 SSH 배포 제한)")

    return ReadyStatus.OK, issues if issues else []


def check_ops_ready() -> tuple[ReadyStatus, list[str]]:
    """
    Ops 도구 준비 상태 확인 (§S-2).

    체크 항목:
    1. ssh 클라이언트 설치 여부 (원격 Docker 접근)
    2. git 설치 여부
    3. docker 설치 여부
    4. DISCORD_WEBHOOK_URL 환경변수 (선택)

    반환: (ReadyStatus, issues_list)
    - OK: ssh + git + docker 모두 사용 가능
    - WARN: 일부 도구 누락 (로컬 배포 일부 기능 제한)
    """
    issues: list[str] = []
    warn = False

    # ssh 클라이언트
    if shutil.which("ssh") is None:
        issues.append("Ops Ready: ssh 미설치 — 원격 Docker 접근 비활성화")
        warn = True

    # git 설치 확인
    if shutil.which("git") is None:
        issues.append("Ops Ready: git 미설치 — git_agent 기능 비활성화")
        warn = True

    # docker 설치 확인
    if shutil.which("docker") is None:
        issues.append("Ops Ready: docker 미설치 — 컨테이너 배포 비활성화")
        warn = True

    # Discord Webhook (선택)
    if not os.environ.get("DISCORD_WEBHOOK_URL"):
        issues.append("Ops Ready: DISCORD_WEBHOOK_URL 미설정 (알림 비활성, 선택사항)")
        # Discord는 선택사항이므로 warn=True로 만들지 않음

    if warn:
        return ReadyStatus.WARN, issues

    return ReadyStatus.OK, issues if issues else []


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def setup_recoder_home() -> None:
    """
    ~/.recoder/ 하위 디렉터리 생성.
    디렉터리: sessions/, backups/, logs/, projects/, templates/, deployments/
    macOS/Linux: chmod 0700
    Windows: ACL 설정 시도, 실패 시 경고만 (Soft Fail)
    """
    RECODER_HOME.mkdir(parents=True, exist_ok=True)

    subdirs = [
        "sessions",
        "backups",
        "logs",
        "projects",
        "templates",
        "deployments",
    ]

    for subdir in subdirs:
        subdir_path = RECODER_HOME / subdir
        subdir_path.mkdir(parents=True, exist_ok=True)

        if sys.platform == "win32":
            try:
                os.system(
                    f'icacls "{subdir_path}" /inheritance:d '
                    f'/grant:r "%USERNAME%:F" >nul 2>&1'
                )
            except Exception:
                pass
        else:
            try:
                subdir_path.chmod(0o700)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_diagnostics(result: DiagnosticsResult) -> None:
    """결과를 ~/.recoder/diagnostics.json에 저장."""
    RECODER_HOME.mkdir(parents=True, exist_ok=True)

    with open(DIAGNOSTICS_PATH, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2)


def load_diagnostics() -> Optional[DiagnosticsResult]:
    """저장된 diagnostics.json 로드. 없으면 None."""
    if not DIAGNOSTICS_PATH.exists():
        return None

    try:
        with open(DIAGNOSTICS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

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
            issues=data.get("issues", []),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Internal utility
# ---------------------------------------------------------------------------

def _detect_bedrock_region() -> Optional[str]:
    """Detect the AWS region configured in the environment or AWS config."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region:
        return region

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

    return None


# ---------------------------------------------------------------------------
# Legacy class wrapper — kept for compatibility with older callers that
# instantiate FirstRunDiagnostics().run_all().
# ---------------------------------------------------------------------------

class FirstRunDiagnostics:
    """
    Compatibility wrapper around the module-level functions.

    Older code paths import this class and call ``run_all()`` to receive
    a DiagnosticsResult. The implementation simply delegates to
    :func:`run_diagnostics`.
    """

    async def run_all(self) -> DiagnosticsResult:
        return await run_diagnostics()

    async def save_diagnostics(self, result: DiagnosticsResult) -> None:
        save_diagnostics(result)

    async def load_diagnostics(self) -> Optional[DiagnosticsResult]:
        return load_diagnostics()

    @staticmethod
    def _detect_bedrock_region() -> Optional[str]:
        return _detect_bedrock_region()
        save_diagnostics(result)

    async def load_diagnostics(self) -> Optional[DiagnosticsResult]:
        return load_diagnostics()

    @staticmethod
    def _detect_bedrock_region() -> Optional[str]:
        return _detect_bedrock_region()
