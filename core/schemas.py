"""
ReCoder v6.4 Core Data Contracts — 설계서 §20

Pydantic v2 schemas for all inter-component data exchange. 모든 에이전트가 이 파일을
import해서 사용한다. 변경 시 HANDOFF.md 업데이트 필수.

This module is the single source of truth for cross-module data contracts:
  - Pydantic models (Section 20.1 ~ 20.11): ProjectProfile, AnalyzeRequest, PatchProposal,
    InfraFileProposal, DeploymentPlan, DeploymentRecord, AlertRecord, ResponseProposal,
    LLMCallRecord, SessionRecord, CommandTemplate, FileTemplate, DiagnosticsResult, ...
  - Orchestrator FSM enums and update payloads (ContextSource, EventType, UserAction,
    OrchestratorState, OrchestratorUpdate, ExtractedContext, AgentEvent, SessionEvent).
  - v5.0 Q1~Q4 extensions: AST chunking, ExecutionPlan, EvalHarness, Observability,
    Incident/RCA, MCP, Final Demo metadata.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


SCHEMA_VERSION = "6.4"


# ===========================================================================
# 표준 시간 헬퍼 — datetime 은 항상 UTC 로 저장.
# 사용자 표시 timezone (KST 등) 은 UI/Standup 레이어에서 변환.
# ===========================================================================

def utc_now() -> datetime:
    """타임존 인식 UTC 현재 시각. Field(default_factory=utc_now) 로 사용."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Compatibility shim — legacy dataclass-style .to_dict() on Pydantic models
#
# 기존 코드(first_run.py, session_logger.py, server.py, risk_validator.py, ...)
# 는 dataclass 버전 모델의 .to_dict() 를 호출하던 습관이 남아있다. Pydantic v2
# 에서는 model_dump() 가 canonical 메서드지만, 매 호출처를 수정하는 것보다
# BaseModel 에 .to_dict() 호환 메서드를 한 번 패치해서 둘 다 동작하게 한다.
#
# - mode="json" 으로 직렬화 → datetime / enum 등이 JSON-safe 형태로 변환된다.
# - 이미 .to_dict() 가 정의된 클래스(dataclass 등)는 영향 받지 않는다.
# - Pydantic 의 self 검증/직렬화 로직은 그대로 사용한다.
# ---------------------------------------------------------------------------

if not hasattr(BaseModel, "to_dict"):
    def _pydantic_to_dict_compat(self) -> dict:  # type: ignore[no-redef]
        """Legacy alias for model_dump(mode='json')."""
        return self.model_dump(mode="json")

    BaseModel.to_dict = _pydantic_to_dict_compat  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalLevel(int, Enum):
    AUTO = 1            # No confirmation required
    CONFIRM = 2         # Single confirmation
    DOUBLE_CONFIRM = 3  # Two-step confirmation
    BLOCKED = 4         # Cannot be executed


class StackType(str, Enum):
    PYTHON_FASTAPI = "python-fastapi"
    PYTHON_FLASK = "python-flask"
    PYTHON_DJANGO = "python-django"
    NODE_EXPRESS = "node-express"
    NODE_NEXT = "node-next"
    NODE_NEST = "node-nest"
    GO = "go"
    JAVA_SPRING = "java-spring"
    RUBY_RAILS = "ruby-rails"
    STATIC = "static"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class DeployMethod(str, Enum):
    """설계 A.5 DeploymentPlan.method — 배포 대상 분류.

    LOCAL_DOCKER : 로컬 Docker 엔진에 컨테이너 띄움 (P0)
    SSH_DIRECT   : SSH 로 원격 호스트에서 docker build + run (P1)
    ECR_EC2      : 로컬에서 ECR push → EC2 가 docker pull + run (P1)
    GITHUB_ACTIONS: GitHub Actions 워크플로우가 CI/CD 수행 (P2)
    SSH_DOCKER   : (legacy alias) — SSH_DIRECT 와 동일
    AWS_ECS      : ECS Service 업데이트 (확장)
    AWS_LAMBDA   : Lambda 배포 (확장)
    K8S          : Kubernetes 배포 (P2)
    """
    LOCAL_DOCKER = "local_docker"
    SSH_DOCKER = "ssh_docker"
    SSH_DIRECT = "ssh_direct"
    ECR_EC2 = "ecr_ec2"
    GITHUB_ACTIONS = "github_actions"
    AWS_ECS = "aws_ecs"
    AWS_LAMBDA = "aws_lambda"
    K8S = "k8s"


class AlertType(str, Enum):
    CRASH = "crash"
    HIGH_CPU = "high_cpu"
    HIGH_MEMORY = "high_memory"
    HEALTH_CHECK_FAIL = "health_check_fail"
    OOM = "oom"
    DEPLOY_FAILURE = "deploy_failure"
    DISK_PRESSURE = "disk_pressure"
    LATENCY_SPIKE = "latency_spike"
    ERROR_RATE_SPIKE = "error_rate_spike"
    CUSTOM = "custom"


class ActionType(str, Enum):
    DOCKER_BUILD = "docker_build"
    DOCKER_RUN = "docker_run"
    DOCKER_STOP = "docker_stop"
    DOCKER_RESTART = "docker_restart"
    DOCKER_LOGS = "docker_logs"
    SSH_DOCKER_RESTART = "ssh_docker_restart"
    SSH_DOCKER_ROLLBACK = "ssh_docker_rollback"
    SSH_ENV_UPDATE = "ssh_env_update"
    ECR_LOGIN = "ecr_login"
    ECR_PUSH = "ecr_push"
    ECR_PULL = "ecr_pull"
    SCALE_UP = "scale_up"
    SCALE_DOWN = "scale_down"
    NOTIFY = "notify"
    NO_ACTION = "no_action"
    # v10 §32 Remediation Extension - CommandTemplate
    SYSTEM_COMMAND = "system_command"
    PACKAGE_INSTALL = "package_install"
    FILE_WRITE = "file_write"
    # ECS / ECR / SBOM 배포 파이프라인 (command_registry)
    DOCKER_REMOVE = "docker_remove"
    DOCKER_TAG_ECR = "docker_tag_ecr"
    DOCKER_PUSH_ECR = "docker_push_ecr"
    ECR_GET_LOGIN_PASSWORD = "ecr_get_login_password"
    ECS_DESCRIBE_SERVICE = "ecs_describe_service"
    ECS_UPDATE_SERVICE = "ecs_update_service"
    SYFT_SBOM = "syft_sbom"


class FileType(str, Enum):
    DOCKERFILE = "dockerfile"
    DOCKER_COMPOSE = "docker_compose"
    GITHUB_ACTIONS = "github_actions"
    NGINX_CONF = "nginx_conf"
    ENV_FILE = "env_file"
    K8S_MANIFEST = "k8s_manifest"
    TERRAFORM = "terraform"
    # v10 §32 Remediation Extension - RemediationProposal FileTemplate
    GITIGNORE = "gitignore"
    PYTHON_SOURCE = "python_source"
    JS_SOURCE = "js_source"
    RECODER_YML = "recoder_yml"
    OTHER = "other"


class ProviderType(str, Enum):
    ANTHROPIC = "anthropic"
    BEDROCK = "bedrock"
    OPENAI = "openai"
    LOCAL = "local"


class ReadyState(str, Enum):
    READY = "ready"
    NOT_READY = "not_ready"
    PARTIAL = "partial"
    ERROR = "error"


class DeployStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Supporting Models (defined first to avoid forward-reference issues)
# ---------------------------------------------------------------------------


class HealthCheckResult(BaseModel):
    """Result of an HTTP health check probe."""

    status: str  # "healthy" | "unhealthy" | "timeout" | "error"
    latency_ms: Optional[int] = Field(default=None, ge=0)
    checked_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Core Domain Models
# ---------------------------------------------------------------------------


class ProjectProfile(BaseModel):
    """Represents a registered workspace/project with its deployment metadata."""

    project_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workspace_path: str
    stack: StackType = StackType.UNKNOWN
    package_manager: Optional[str] = None  # pip, npm, yarn, pnpm, cargo, etc.
    default_run_command: Optional[str] = None
    default_port: Optional[int] = Field(default=None, ge=1, le=65535)
    health_check_path: str = "/health"
    dockerfile_path: Optional[str] = None
    compose_path: Optional[str] = None
    deployment_target: DeployMethod = DeployMethod.LOCAL_DOCKER
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AnalyzeRequest(BaseModel):
    """Payload sent to the LLM analysis endpoint from the extension."""

    workspace_path: str
    active_file_path: Optional[str] = None
    selected_text: Optional[str] = None
    terminal_output: Optional[str] = None
    command: Optional[str] = None
    project_files_summary: Optional[str] = None
    project_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Patch & Proposal Models
# ---------------------------------------------------------------------------


class FilePatch(BaseModel):
    """A single file change expressed as a unified diff."""

    file: str  # Relative path within the workspace
    base_sha256: Optional[str] = None  # SHA-256 of the original file content
    unified_diff: str  # Unified diff string
    reason: str  # Human-readable explanation for this change


class PatchProposal(BaseModel):
    """A set of file patches proposed by the AI code agent."""

    schema_version: str = "1.0"
    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    summary: str
    risk_level: RiskLevel = RiskLevel.LOW
    risk_reasons: list[str] = Field(default_factory=list)
    approval_level: ApprovalLevel = ApprovalLevel.CONFIRM
    patches: list[FilePatch]
    test_command: Optional[str] = None


class InfraFileProposal(BaseModel):
    """A proposal to create or update an infrastructure file."""

    schema_version: str = "1.0"
    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    file_type: FileType
    target_path: str  # Relative path within the workspace
    # 워크스페이스 루트(절대경로). approve 시 target_path 를 이 경로 기준으로 해석.
    # 없으면(레거시) approve 단계가 cwd(=core/) 폴백 -> 잘못된 위치에 써지는 버그.
    workspace_path: Optional[str] = None
    content: str
    base_template: Optional[str] = None  # template_id used as base
    required_secrets: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    risk_reasons: list[str] = Field(default_factory=list)
    approval_level: ApprovalLevel = ApprovalLevel.CONFIRM


# ---------------------------------------------------------------------------
# Deployment Models
# ---------------------------------------------------------------------------


class DeploymentPlan(BaseModel):
    """An executable deployment plan produced by the deploy agent."""

    schema_version: str = "1.0"
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    method: DeployMethod
    action: ActionType
    image: Optional[str] = None
    container_name: Optional[str] = None
    ports: dict[str, str] = Field(default_factory=dict)  # host_port -> container_port
    env: dict[str, str] = Field(default_factory=dict)
    health_check_path: str = "/health"
    rollback_image: Optional[str] = None
    command_template_id: Optional[str] = None
    risk_level: RiskLevel = RiskLevel.MEDIUM
    risk_reasons: list[str] = Field(default_factory=list)
    approval_level: ApprovalLevel = ApprovalLevel.CONFIRM


class DeploymentRecord(BaseModel):
    """Immutable record of a completed deployment event."""

    deployment_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    method: DeployMethod
    image: str
    image_digest: Optional[str] = None
    git_commit: Optional[str] = None
    container_name: str
    health_check_path: str = "/health"
    deployed_at: datetime = Field(default_factory=datetime.utcnow)
    rollback_target: Optional[str] = None  # Previous image tag for rollback
    status: DeployStatus = DeployStatus.SUCCESS


# ---------------------------------------------------------------------------
# Alerting & Ops Models
# ---------------------------------------------------------------------------


class AlertRecord(BaseModel):
    """An ops alert captured by the watchdog or an external source."""

    alert_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str  # e.g., "watchdog", "cloudwatch", "prometheus"
    project_id: Optional[str] = None
    environment: str = "production"
    host: Optional[str] = None
    container_name: Optional[str] = None
    alert_type: AlertType
    severity: RiskLevel
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    logs_excerpt: Optional[str] = None
    health_check_result: Optional[HealthCheckResult] = None
    metric_snapshot: dict[str, Any] = Field(default_factory=dict)
    recent_deployment_id: Optional[str] = None
    fingerprint: Optional[str] = None  # Dedup key
    mask_version: Optional[str] = None  # Version of masking applied to logs


class ResponseProposal(BaseModel):
    """AI-generated remediation proposal for an ops alert."""

    schema_version: str = "1.0"
    alert_id: str
    action_type: ActionType
    target_container: Optional[str] = None
    command_template_id: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    risk_reasons: list[str] = Field(default_factory=list)
    approval_level: ApprovalLevel = ApprovalLevel.CONFIRM


# ---------------------------------------------------------------------------
# LLM & Session Tracking
# ---------------------------------------------------------------------------


class LLMCallRecord(BaseModel):
    """Telemetry record for a single LLM API call."""

    call_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent: str  # e.g., "code_agent", "deploy_agent", "ops_agent"
    operation: str  # e.g., "analyze", "plan_deploy", "diagnose_alert"
    provider: ProviderType
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0.0)
    latency_ms: int = Field(ge=0)
    token_source: str = "api"  # "api" | "cache"
    fallback_used: bool = False
    retry_count: int = Field(default=0, ge=0)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SessionRecord(BaseModel):
    """Full session record aggregating all events and LLM calls."""

    schema_version: str = "1.0"
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    start_time: datetime = Field(default_factory=datetime.utcnow)
    end_time: Optional[datetime] = None
    project_id: Optional[str] = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    llm_calls: list[LLMCallRecord] = Field(default_factory=list)
    llm_usage_summary: dict[str, Any] = Field(default_factory=dict)
    raw_content_saved: bool = False


# ---------------------------------------------------------------------------
# Registry Models
# ---------------------------------------------------------------------------


class CommandTemplate(BaseModel):
    """A safe, parameterized command template for execution."""

    template_id: str
    action_type: ActionType
    allowed_params: dict[str, Any]  # param_name -> validation rules
    command_pattern: str
    risk_level: RiskLevel
    approval_level: ApprovalLevel
    version: str = "1.0"

    @field_validator("allowed_params", mode="before")
    @classmethod
    def _coerce_allowed_params(cls, v):
        """일부 레지스트리가 파라미터 이름 list 를 넘기므로 {name: {}} dict 로 변환."""
        if isinstance(v, (list, tuple, set)):
            return {str(k): {} for k in v}
        return v


class FileTemplate(BaseModel):
    """A reusable infrastructure file template."""

    template_id: str
    file_type: FileType
    base_content: str
    customizable_sections: dict[str, str] = Field(default_factory=dict)
    version: str = "1.0"

    @field_validator("file_type", mode="before")
    @classmethod
    def _normalize_file_type(cls, v):
        """레지스트리별로 다른 file_type 표기를 FileType enum 값으로 정규화."""
        if isinstance(v, str):
            key = v.strip().lower()
            alias = {
                "dockerfile":          "dockerfile",
                "docker-compose":      "docker_compose",
                "github-actions":      "github_actions",
                "argocd-application":  "k8s_manifest",
                "helm-values":         "other",
                "ecs-task-definition": "other",
            }
            return alias.get(key, key)
        return v

    @field_validator("customizable_sections", mode="before")
    @classmethod
    def _coerce_customizable_sections(cls, v):
        """일부 레지스트리가 섹션 이름 list 를 넘기므로 dict 로 변환."""
        if isinstance(v, (list, tuple, set)):
            return {str(s): "" for s in v}
        return v


# ---------------------------------------------------------------------------
# Diagnostic & Runtime Models
# ---------------------------------------------------------------------------


class DiagnosticsResult(BaseModel):
    """Result of the /diagnostics endpoint — system readiness check (§11).

    Field types align with actual values produced by ``first_run.py``.
    Ready 상태와 provider_type 은 string 으로 정의 — Pydantic v2 의
    enum 호환 경고("serialized value may not be as expected")를 차단.

    String 으로 받아도 first_run 이 ReadyStatus enum 을 직접 할당하면
    Pydantic 이 .value 로 자동 coerce (ReadyStatus, ReadyState 둘 다 str Enum).
    """

    # Ready 상태 — first_run 이 ReadyStatus.OK 등을 직접 할당, str Enum 이므로 자동 coerce
    core_ready: str = "fail"
    ai_ready: str = "fail"
    docker_ready: str = "fail"
    aws_deploy_ready: str = "fail"
    ops_ready: str = "fail"
    resolved_model_id: Optional[str] = None
    resolved_region: Optional[str] = None
    is_cross_region_profile: bool = False
    # provider_type 은 자유 문자열 ("bedrock"/"gemini"/"anthropic" 등)
    provider_type: Optional[str] = None
    # ISO-8601 datetime string — first_run.py 가 .isoformat() 으로 채움
    validation_time: str = ""
    details: dict[str, Any] = Field(default_factory=dict)

    # 진단 중 수집된 사유/경고 목록 — first_run.py 가 누적해서 채운다.
    issues: list[str] = Field(default_factory=list)
    # Docker 버전 문자열 (예: "Docker version 24.0.7, build afdd53b") — Docker Ready 진단 시 채움.
    docker_version: str = ""

    model_config = {
        "use_enum_values": True,  # ReadyStatus.OK → "ok" 자동 직렬화
    }


class RuntimeConfig(BaseModel):
    """Runtime state persisted to .recode_runtime.json."""

    port: int = Field(ge=1, le=65535)
    session_token: str
    pid: int
    started_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Utility / Supporting Models
# ---------------------------------------------------------------------------


class MaskingResult(BaseModel):
    """Result of applying PII/secret masking to content."""

    masked_content: str
    mask_count: int = Field(ge=0)
    mask_version: str


class QualityScore(BaseModel):
    """Quality heuristics for alert log content."""

    score: float = Field(ge=0.0, le=1.0)
    has_traceback: bool = False
    has_project_path: bool = False
    error_message_length: int = Field(ge=0)
    masked_info_density: float = Field(ge=0.0, le=1.0)
    has_related_files: bool = False


class CostSummary(BaseModel):
    """Rolling cost summary for LLM usage."""

    daily_usd: float = Field(ge=0.0)
    monthly_usd: float = Field(ge=0.0)
    call_count: int = Field(ge=0)
    last_updated: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# v5.0 Q1 — AST Chunking (설계서 §Q1)
#
# 인덱스에는 ChunkMetadata만 저장 (source text 미포함, ADR-004).
# chunk_id = SHA-256(file_path + name + str(start_line)) 앞 8자리.
# 청크 길이 상한 1500 토큰, 청크 오버랩 없음.
# ---------------------------------------------------------------------------


class NodeType(str, Enum):
    """AST 청크 노드 종류 (Python ast 기반 + JS line-based fallback)."""

    MODULE         = "module"
    FUNCTION       = "function"
    ASYNC_FUNCTION = "async_function"
    CLASS          = "class"


class ChunkMetadata(BaseModel):
    """
    인덱싱 단위 청크의 메타데이터.

    source text 는 절대 포함하지 않는다 (ADR-004).
    필요 시 LLM 전달 직전에 file_path 에서 다시 읽고 Context Gate 를 통과시킨다.
    """

    chunk_id:       str  # SHA-256(file_path + name + str(start_line))[:8]
    file_path:      str
    node_type:      NodeType
    name:           str
    start_line:     int = Field(ge=1)
    end_line:       int = Field(ge=1)
    token_estimate: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# v5.0 Q1 — Plan-Execute-Verify (설계서 §Plan-Execute-Verify)
#
# PlannerAgent: Bedrock Sonnet, 최대 5단계 ExecutionPlan, Structured Output, 실행 금지.
# Executor: LLM 아님. action 타입에 따라 CodeAgent/InfraAgent/DeployAgent/TestRunner 호출.
# VerifierAgent: LLM 없음. Schema validation + base_sha256 + test_command.
# 재시도 최대 2회, 소진 시 수동 검토.
# ---------------------------------------------------------------------------


class AgentType(str, Enum):
    """ExecutionStep 이 디스패치할 결정론적 에이전트 종류."""

    CODE_AGENT   = "code_agent"
    INFRA_AGENT  = "infra_agent"
    DEPLOY_AGENT = "deploy_agent"
    TEST_RUNNER  = "test_runner"
    NO_OP        = "no_op"


class ExecutionStep(BaseModel):
    """ExecutionPlan 의 한 단계. Executor 가 결정론적으로 디스패치한다."""

    step_id:     str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    description: str = ""
    agent:       AgentType = AgentType.CODE_AGENT
    args:        dict[str, Any] = Field(default_factory=dict)
    depends_on:  list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    """PlannerAgent 가 생성하는 최대 5단계 실행 계획 (Structured Output)."""

    schema_version:    str = "1.0"
    plan_id:           str = Field(default_factory=lambda: str(uuid.uuid4()))
    summary:           str = "AI-generated execution plan"
    estimated_risk:    RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    steps:             list[ExecutionStep] = Field(default_factory=list, max_length=5)


class VerificationResult(BaseModel):
    """VerifierAgent 결과 — LLM 미사용. retry_count 가 _MAX_RETRIES 도달 시 needs_manual_review."""

    plan_id:             str
    proposal_id:         str
    retry_count:         int = Field(default=0, ge=0)
    schema_valid:        bool = False
    sha256_valid:        bool = False
    test_passed:         Optional[bool] = None  # None = test_command 미지정
    test_output:         Optional[str] = None
    needs_manual_review: bool = False


# ---------------------------------------------------------------------------
# v5.0 Q1 — Eval Harness (설계서 §Eval Harness)
#
# Safety violation 0건 CI 강제. pass_rate >= 60% (Q1) / 80% (Q2).
# 카테고리 6개: python_single_file, python_multi_file, nodejs_error,
#               dockerfile_generation, docker_build_failure, health_check_failure.
# ---------------------------------------------------------------------------


class SafetyViolationType(str, Enum):
    """Demo Release Gate 의 5종 안전 위반 (각 0건 필수)."""

    SECRET_LEAK              = "secret_leak"
    NONEXISTENT_IMPORT       = "nonexistent_import"
    INVALID_SHELL_COMMAND    = "invalid_shell_command"
    DESTRUCTIVE_OPERATION    = "destructive_operation"
    ROLLBACK_NOT_DISCLOSED   = "rollback_not_disclosed"


class EvalCategory(str, Enum):
    """Eval Harness 6 카테고리 (설계서 §Eval Harness)."""

    PYTHON_SINGLE_FILE     = "python_single_file"
    PYTHON_MULTI_FILE      = "python_multi_file"
    NODEJS_ERROR           = "nodejs_error"
    DOCKERFILE_GENERATION  = "dockerfile_generation"
    DOCKER_BUILD_FAILURE   = "docker_build_failure"
    HEALTH_CHECK_FAILURE   = "health_check_failure"


class EvalCase(BaseModel):
    """단일 평가 케이스 (cases/*.json 에서 로드)."""

    case_id:                       str
    category:                      EvalCategory
    description:                   str = ""
    workspace_snapshot:            dict[str, str] = Field(default_factory=dict)
    terminal_output:               str = ""
    command:                       Optional[str] = None
    expected_files_changed:        list[str] = Field(default_factory=list)
    expected_no_safety_violations: bool = True
    expected_patch_keywords:       list[str] = Field(default_factory=list)
    tags:                          list[str] = Field(default_factory=list)


class EvalResult(BaseModel):
    """단일 EvalCase 실행 결과."""

    case_id:            str
    category:           EvalCategory
    passed:             bool
    safety_violations:  list[SafetyViolationType] = Field(default_factory=list)
    proposal_summary:   Optional[str] = None
    patch_files:        list[str] = Field(default_factory=list)
    error_message:      Optional[str] = None
    duration_seconds:   float = Field(default=0.0, ge=0.0)

    @property
    def has_safety_violation(self) -> bool:
        return len(self.safety_violations) > 0


class EvalReport(BaseModel):
    """전체 Eval 실행 보고. CI gate = safety_violations == 0 AND pass_rate >= threshold."""

    schema_version:   str = "1.0"
    total:            int = Field(ge=0)
    passed:           int = Field(ge=0)
    failed:           int = Field(ge=0)
    safety_violations: int = Field(default=0, ge=0)
    pass_rate:        float = Field(default=0.0, ge=0.0, le=1.0)
    ci_gate_passed:   bool = False
    by_category:      dict[str, dict[str, Any]] = Field(default_factory=dict)
    results:          list[EvalResult] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# v5.0 Q4 — Observability / Incident / RCA Models
#
# 설계서 §Q4 — OpenTelemetry 통합 + Incident Correlation + RCA MVP + Postmortem
# 핵심 원칙:
#  - RCA 는 "확정 원인" 을 말하지 않는다. confidence score 가 항상 함께한다.
#  - correlation score 가 낮으면 "최근 배포와 직접 관련성 낮음" 으로 표기한다.
#  - OTel 미연결 시 Watchdog incident.jsonl + AuditLog 만으로 fallback 한다.
# ---------------------------------------------------------------------------


class IncidentSeverity(str, Enum):
    """Incident severity (ADR-005 — Severity 1 은 emergency rollback 대상)."""

    SEV1 = "sev1"   # critical: production 중단, 즉시 대응
    SEV2 = "sev2"   # high   : 주요 기능 마비
    SEV3 = "sev3"   # medium : 부분 영향
    SEV4 = "sev4"   # low    : 관측만, 대응 옵션


class IncidentEventKind(str, Enum):
    """Incident Timeline 의 단위 이벤트 종류."""

    DEPLOYMENT     = "deployment"
    ALERT          = "alert"
    METRIC_SPIKE   = "metric_spike"
    LOG_PATTERN    = "log_pattern"
    HEALTH_CHECK   = "health_check"
    APPROVAL       = "approval"
    ROLLBACK       = "rollback"
    GITOPS_PR      = "gitops_pr"
    OTEL_SPAN      = "otel_span"
    AUDIT          = "audit"
    NOTE           = "note"


class CorrelationSignalKind(str, Enum):
    """Incident Correlation 의 8개 신호 (설계서 §Q4 Incident Correlation 설계)."""

    ERROR_RATE_DELTA       = "error_rate_delta"
    LATENCY_DELTA          = "latency_delta"
    CHANGED_FILES_AREA     = "changed_files_area"
    CONTAINER_RESTART      = "container_restart"
    HEALTH_CHECK_FAILURE   = "health_check_failure"
    LOG_KEYWORD_DELTA      = "log_keyword_delta"
    TRAFFIC_SPIKE          = "traffic_spike"
    DEPENDENCY_ERROR       = "dependency_error"


class IncidentEvent(BaseModel):
    """Timeline 위에 표시되는 한 줄의 이벤트."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    incident_id: str
    occurred_at: datetime
    kind: IncidentEventKind
    title: str
    detail: Optional[str] = None
    source: str = "unknown"  # 예: "otel", "watchdog", "auditlog", "deployment_record"
    refs: dict[str, Any] = Field(default_factory=dict)  # 자유 형식 메타


class IncidentTimeline(BaseModel):
    """Incident Timeline MVP — 시간 정렬된 이벤트의 집합."""

    schema_version: str = "1.0"
    incident_id: str
    project_id: Optional[str] = None
    severity: IncidentSeverity = IncidentSeverity.SEV3
    detected_at: datetime
    resolved_at: Optional[datetime] = None
    events: list[IncidentEvent] = Field(default_factory=list)
    otel_available: bool = False
    fallback_reason: Optional[str] = None  # OTel 미연결 시 사유 텍스트


class ObservabilityQueryKind(str, Enum):
    METRIC = "metric"
    LOG    = "log"
    TRACE  = "trace"


class ObservabilityQueryResult(BaseModel):
    """PrometheusAdapter / LokiAdapter 의 통합 응답."""

    schema_version: str = "1.0"
    kind: ObservabilityQueryKind
    query: str
    started_at: datetime
    ended_at: datetime
    samples: list[dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None
    backend: str = "unknown"  # "prometheus" | "loki" | "tempo" | "fallback"


class CorrelationSignal(BaseModel):
    """단일 신호와 그 기여도."""

    kind: CorrelationSignalKind
    weight: float = Field(ge=0.0, le=1.0)
    score: float = Field(ge=0.0, le=1.0)  # 정규화된 신호 강도
    evidence: str
    raw: dict[str, Any] = Field(default_factory=dict)


class CorrelationResult(BaseModel):
    """한 인시던트 ↔ 한 DeploymentRecord 사이의 매칭 결과."""

    schema_version: str = "1.0"
    incident_id: str
    candidate_deployment_id: Optional[str] = None
    candidate_image_tag: Optional[str] = None
    signals: list[CorrelationSignal] = Field(default_factory=list)
    correlation_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    label: str = "candidate"  # "candidate" | "weak_link" | "no_link"
    rationale: str = ""


class RCASymptom(BaseModel):
    """관측된 증상 한 줄 (RCA 구조화 출력 #3)."""

    name: str             # "error_rate", "memory", "restart_count", "health_check_failure"
    value: Optional[str] = None
    delta: Optional[str] = None
    evidence: Optional[str] = None


class RCACandidate(BaseModel):
    """가능성 높은 원인 후보 (RCA 구조화 출력 #4).

    표현 원칙 (설계서):
      - "원인입니다" 사용 금지 — 항상 "가능성 높은 원인 후보입니다" 톤
      - confidence 가 항상 함께 표시된다
    """

    candidate_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    hypothesis: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rollback_hint: Optional[str] = None
    related_files: list[str] = Field(default_factory=list)


class RCAReport(BaseModel):
    """RCA MVP — 설계서 §Q4 "구조화 출력 4가지" 만 채우면 Must 충족."""

    schema_version: str = "1.0"
    rca_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    incident_id: str
    generated_at: datetime = Field(default_factory=datetime.utcnow)

    # ① 가장 의심되는 배포 이벤트와 근거
    suspected_deployment_id: Optional[str] = None
    suspected_deployment_reason: str = ""

    # ② 관련 변경 파일 목록
    related_files: list[str] = Field(default_factory=list)

    # ③ 관측된 증상
    symptoms: list[RCASymptom] = Field(default_factory=list)

    # ④ 가능성 높은 원인 후보 1~3 개와 각각의 근거
    candidates: list[RCACandidate] = Field(default_factory=list)

    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    insufficient_evidence: bool = False
    disclaimer: str = (
        "본 RCA 는 확정된 원인이 아니라 가능성 높은 원인 후보 목록입니다. "
        "최종 판단은 담당 엔지니어가 수행해야 합니다."
    )


class PostmortemSection(BaseModel):
    """Postmortem 한 섹션."""

    heading: str
    body: str
    auto_filled: bool = True


class PostmortemSkeleton(BaseModel):
    """Postmortem skeleton 자동 생성 결과 (설계서 §Q4 템플릿 일치)."""

    schema_version: str = "1.0"
    postmortem_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    incident_id: str
    project_id: Optional[str] = None
    severity: IncidentSeverity = IncidentSeverity.SEV3
    sections: list[PostmortemSection] = Field(default_factory=list)
    markdown_path: Optional[str] = None
    otel_available: bool = False


# ---------------------------------------------------------------------------
# v5.0 Q4 — MCP server PoC (local stdio)
# ---------------------------------------------------------------------------


class MCPTransport(str, Enum):
    STDIO              = "stdio"
    LOCAL_HTTP         = "local_http"
    STREAMABLE_HTTP    = "streamable_http"  # Backlog


class MCPToolDescriptor(BaseModel):
    """MCP `tools/list` 응답 단위."""

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)


class MCPInvocation(BaseModel):
    """MCP `tools/call` 입력."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class MCPInvocationResult(BaseModel):
    """MCP `tools/call` 출력 (텍스트 결과만 우선 지원)."""

    is_error: bool = False
    content: list[dict[str, Any]] = Field(default_factory=list)  # MCP content blocks


# ---------------------------------------------------------------------------
# v5.0 Q4 — Final Demo metadata
# ---------------------------------------------------------------------------


class DemoStepStatus(str, Enum):
    PENDING    = "pending"
    RUNNING    = "running"
    PASSED     = "passed"
    FAILED     = "failed"
    SKIPPED    = "skipped"


class DemoStep(BaseModel):
    index: int = Field(ge=1)
    title: str
    description: str
    expected_duration_sec: int = Field(ge=0)
    status: DemoStepStatus = DemoStepStatus.PENDING


class DemoScenario(BaseModel):
    """Final Demo B 의 10단계 시나리오 (설계서 §Final Demo)."""

    name: str = "final_demo_b"
    cluster_provider: str = "eks"  # ADR-009: k3d/kind 금지
    cluster_lifetime_minutes: int = 120
    steps: list[DemoStep] = Field(default_factory=list)


# ===========================================================================
# Q4 Compatibility Aliases & Missing Schemas
# (우리 Q4 에이전트 코드가 사용하는 클래스 — 팀원 스키마와 브리지)
# ===========================================================================

from typing import Optional as _Opt


# ---------------------------------------------------------------------------
# ArgoCD GitOps (팀원 schemas에 없음 → 추가)
# ---------------------------------------------------------------------------

class ArgoSyncPhase(str, Enum):
    UNKNOWN    = "Unknown"
    SYNCED     = "Synced"
    OUT_OF_SYNC = "OutOfSync"
    SYNC_FAILED = "SyncFailed"


class ArgoHealthStatus(str, Enum):
    UNKNOWN     = "Unknown"
    PROGRESSING = "Progressing"
    HEALTHY     = "Healthy"
    SUSPENDED   = "Suspended"
    DEGRADED    = "Degraded"
    MISSING     = "Missing"


class ArgoSyncRequest(BaseModel):
    project_id:       str
    app_name:         str
    argocd_server:    str
    argocd_token:     str
    target_revision:  _Opt[str] = "HEAD"
    prune:            bool = False
    dry_run:          bool = False
    force:            bool = False


class ArgoSyncRecord(BaseModel):
    sync_id:           str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:        str
    app_name:          str
    argocd_server:     str
    sync_phase:        ArgoSyncPhase = ArgoSyncPhase.UNKNOWN
    health_status:     ArgoHealthStatus = ArgoHealthStatus.UNKNOWN
    target_revision:   _Opt[str] = None
    live_revision:     _Opt[str] = None
    resources_synced:  int = 0
    resources_failed:  int = 0
    error_message:     _Opt[str] = None
    rollback_triggered: bool = False
    rollback_revision:  _Opt[str] = None
    started_at:        datetime = Field(default_factory=datetime.utcnow)
    completed_at:      _Opt[datetime] = None


# ---------------------------------------------------------------------------
# Incident (팀원: IncidentTimeline / IncidentEvent — 우리 에이전트용 alias)
# ---------------------------------------------------------------------------

class IncidentStatus(str, Enum):
    OPEN         = "open"
    INVESTIGATING = "investigating"
    IDENTIFIED   = "identified"
    MONITORING   = "monitoring"
    RESOLVED     = "resolved"


class TimelineEvent(BaseModel):
    """단위 타임라인 이벤트 (팀원 IncidentEvent alias)."""
    event_id:               str = Field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at:            datetime
    source:                 str
    title:                  str
    description:            str
    severity:               _Opt[IncidentSeverity] = None
    related_deployment_id:  _Opt[str] = None
    related_commit_sha:     _Opt[str] = None
    metadata:               dict = Field(default_factory=dict)


class IncidentRecord(BaseModel):
    incident_id:    str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:     str
    title:          str
    severity:       IncidentSeverity
    status:         IncidentStatus = IncidentStatus.OPEN
    detected_at:    datetime = Field(default_factory=datetime.utcnow)
    resolved_at:    _Opt[datetime] = None
    timeline:       list[TimelineEvent] = Field(default_factory=list)
    rca_candidates: list[RCACandidate] = Field(default_factory=list)
    postmortem_path: _Opt[str] = None
    created_by:     str = "system"


# ---------------------------------------------------------------------------
# Rollback PR (팀원 schemas에 없음 → 추가)
# ---------------------------------------------------------------------------

class RollbackPRRequest(BaseModel):
    project_id:          str
    repo_owner:          str
    repo_name:           str
    target_commit_sha:   str
    github_token:        str
    base_branch:         str = "main"
    pr_title:            _Opt[str] = None
    pr_body:             _Opt[str] = None
    auto_merge:          bool = False
    approval_request_id: _Opt[str] = None


class RollbackPRRecord(BaseModel):
    pr_id:               str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:          str
    repo_full_name:      str
    pr_number:           _Opt[int] = None
    pr_url:              _Opt[str] = None
    target_commit_sha:   str
    revert_branch:       str
    status:              str = "pending"
    error_message:       _Opt[str] = None
    created_at:          datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Observability (팀원 schemas에 없음 → 추가)
# ---------------------------------------------------------------------------

class MetricPoint(BaseModel):
    name:      str
    value:     float
    labels:    dict[str, str] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    unit:      str = ""


class TraceSpan(BaseModel):
    span_id:        str = Field(default_factory=lambda: str(uuid.uuid4()))
    trace_id:       str
    parent_span_id: _Opt[str] = None
    name:           str
    service_name:   str
    start_time:     datetime
    end_time:       _Opt[datetime] = None
    status:         str = "ok"
    attributes:     dict = Field(default_factory=dict)
    error_message:  _Opt[str] = None


class ObservabilityConfig(BaseModel):
    otel_endpoint:   str = "http://localhost:4317"
    prometheus_port: int = 9090
    loki_url:        str = "http://localhost:3100"
    service_name:    str = "recoder-local-core"
    service_version: str = "1.0.0"
    enabled:         bool = True


# ---------------------------------------------------------------------------
# MCP (팀원: MCPToolDescriptor → 우리 에이전트용 alias 추가)
# ---------------------------------------------------------------------------

MCPToolDefinition = MCPToolDescriptor  # alias


class MCPRequest(BaseModel):
    jsonrpc: str = "2.0"
    id:      _Opt[str] = None
    method:  str
    params:  dict = Field(default_factory=dict)


class MCPResponse(BaseModel):
    jsonrpc: str = "2.0"
    id:      _Opt[str] = None
    result:  _Opt[dict] = None
    error:   _Opt[dict] = None


class MCPServerConfig(BaseModel):
    server_name:    str = "recoder-mcp"
    server_version: str = "1.0.0"
    transport:      str = "stdio"
    tools:          list[MCPToolDescriptor] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Q3 Preflight (누락 보완)
# ---------------------------------------------------------------------------

class PreflightCheck(BaseModel):
    """Q3 ECS Cloud Preflight 개별 점검 결과 (preflight_agent 사용)."""
    name:      str
    passed:    bool = True
    severity:  str = "error"          # "error" | "warning"
    detail:    Optional[str] = None
    fix_guide: Optional[str] = None
    # legacy 호환 필드 (deprecated, 일부 코드가 status/message 기대 시 대비)
    status:    Optional[str] = None
    message:   Optional[str] = None


class PreflightReport(BaseModel):
    """Q3 ECS Cloud Preflight 종합 리포트 (preflight_agent 사용)."""
    region:   Optional[str] = None
    cluster:  Optional[str] = None
    service:  Optional[str] = None
    passed:   bool = False
    checks:   list[PreflightCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors:   list[str] = Field(default_factory=list)

    def compute_pass(self) -> bool:
        """error severity 실패 체크가 하나라도 있으면 미통과. warnings/errors 목록을 채운다."""
        errs  = [c for c in self.checks if c.passed is False and c.severity == "error"]
        warns = [c for c in self.checks if c.passed is False and c.severity == "warning"]
        self.errors   = [c.detail or c.name for c in errs]
        self.warnings = [c.detail or c.name for c in warns]
        self.passed   = len(errs) == 0
        return self.passed


# ---------------------------------------------------------------------------
# Q3 Security Scan
# ---------------------------------------------------------------------------

class SecurityScanSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"
    INFO     = "info"


class SecurityScanTool(str, Enum):
    TRIVY    = "trivy"
    HADOLINT = "hadolint"
    GITLEAKS = "gitleaks"


class SecurityFinding(BaseModel):
    tool:        SecurityScanTool
    severity:    SecurityScanSeverity
    rule_id:     Optional[str] = None
    title:       str
    description: Optional[str] = None
    location:    Optional[str] = None
    #: 조치 방법. security_scan.py 는 처음부터 이 값을 채워 보내고 있었는데
    #: 모델에 자리가 없어 pydantic 이 조용히 버렸다 (extra="ignore" 기본값).
    #: 지금 이 값을 화면에 그리는 곳은 없지만, 스캐너가 만들어 놓은 정보를
    #: 모델에서 버리는 상태는 그 자체로 함정이다 — 나중에 쓰려는 사람은
    #: "값을 넣었는데 왜 안 오지"로 시간을 쓰게 된다.
    fix_suggestion: Optional[str] = None
    redacted:    bool = False


#: "스캔을 못 했다"는 뜻의 finding 제목. 이건 취약점이 아니라 **검사 부재**이므로
#: 차단 사유로 세지 않되, tool_errors 로 올려 사용자가 "0건 = 안전"으로
#: 오해하지 않게 한다.
_SCAN_NOT_PERFORMED_TITLES = frozenset({
    "trivy_not_installed", "trivy_scan_failed",
    "hadolint_not_installed", "hadolint_scan_failed",
    "gitleaks_not_installed", "gitleaks_scan_failed",
})

#: 이 중 하나라도 못 돌면 **배포를 막는다.** 배포를 게이트하는 이미지
#: 취약점 검사가 실행되지 못한 경우다. 소스 검사(hadolint·gitleaks)는
#: 자문이라 여기 넣지 않는다 (`compute_pass` 주석 참고).
_IMAGE_SCAN_REQUIRED_TITLES = frozenset({
    "trivy_not_installed", "trivy_scan_failed",
})


class SecurityScanResult(BaseModel):
    """Trivy·Hadolint·gitleaks 통합 결과.

    차단 규칙(설계서 §Q3):
      - Trivy critical → 차단 / Trivy high → 경고
      - Hadolint error → 차단 / warning    → 경고
      - 시크릿         → 항상 차단

    집계값은 **findings 에서 파생**한다. 따로 저장하면 findings 를 고친 뒤
    숫자만 옛날 값으로 남는 어긋남이 생긴다.

    `passed` 는 직접 넣지 말고 `compute_pass()` 로 계산한다. 기본값이 True 인
    이유는 스캐너가 결과 객체를 **먼저 만들고** findings 를 채운 뒤
    compute_pass() 를 부르기 때문이다 — 여기가 required 라서 스캐너가
    구성 시점에 ValidationError 로 죽고 있었다.
    """
    passed:      bool = True
    blocked:     bool = False
    findings:    list[SecurityFinding] = Field(default_factory=list)
    tool_errors: list[str] = Field(default_factory=list)
    scanned_at:  str = Field(default_factory=lambda: __import__('datetime').datetime.utcnow().isoformat())

    #: 무엇을 대상으로 돌렸는가. 셋 다 None 이면 아무것도 검사하지 않은 것이다.
    image:           Optional[str] = None
    dockerfile_path: Optional[str] = None
    repo_path:       Optional[str] = None

    # ------------------------------------------------------------------
    # 집계 — computed_field 로 선언해 model_dump()/JSON 응답에도 실린다.
    # (순수 property 로 두면 서버가 dict 를 손으로 조립하는 경로에서만 보이고,
    #  레코드를 통째로 직렬화하는 경로에서는 조용히 사라진다.)
    # ------------------------------------------------------------------

    @computed_field  # type: ignore[prop-decorator]
    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings
                   if f.tool == SecurityScanTool.TRIVY
                   and f.severity == SecurityScanSeverity.CRITICAL)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings
                   if f.tool == SecurityScanTool.TRIVY
                   and f.severity == SecurityScanSeverity.HIGH)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hadolint_error_count(self) -> int:
        """hadolint level=error 건수. _run_hadolint 가 error 를 CRITICAL 로 올린다."""
        return sum(1 for f in self.findings
                   if f.tool == SecurityScanTool.HADOLINT
                   and f.severity == SecurityScanSeverity.CRITICAL)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def secret_count(self) -> int:
        """시크릿 탐지 건수. gitleaks 와 내장 폴백 스캐너가 같은 제목을 쓴다."""
        return sum(1 for f in self.findings
                   if (f.title or "").startswith("secret_leak:"))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def scan_passed(self) -> bool:
        """`passed` 의 별칭. ECS 경로가 이 이름으로 읽는다."""
        return self.passed

    def compute_pass(self) -> bool:
        """findings 로부터 차단 여부를 확정한다.

        **이미지 취약점 검사가 실행되지 못했으면 통과가 아니다.** trivy 가
        없거나, 시간이 초과되거나, ECR 이미지를 못 받아오면 스캐너는
        `trivy_*` 흔적만 남긴다. 취약점이 **관측되지 않은** 것이지 **없는**
        것이 아니다. 그런데 예전 계산은 그 경우에도 `passed=True` 를 줬다 —
        한 번도 들여다보지 않은 이미지가 게이트를 통과했다.

        배포 계약은 "이미지 스캔이 배포를 막는다"이다. 스캔이 못 돌았으면
        막을 근거를 못 만든 것이므로, **fail-closed** 로 막는다.
        `run_security_scan=False` 를 폴백에서 막은 것과 같은 규칙 — 검사하지
        않은 것을 "위반 없음"으로 바꾸지 않는다.

        ## 왜 trivy 만인가

        빌드 **전** 소스 검사(hadolint·gitleaks)는 의도적으로 **자문(advisory)**
        이다 — 개발 PC 에 그 도구가 없어도 배포는 되게 하고, 못 돌린 것은
        `scan_warning` 으로 표면화한다. 여기서 그것까지 막으면 도구 없는
        PC 에서는 아무도 배포를 못 한다. 그래서 **배포를 게이트하는 이미지
        스캔(trivy)** 이 못 돈 경우만 막는다. 이미지 스캐너를 더 추가하면
        그 미실행 표식도 아래 목록에 넣어야 한다.
        """
        self.tool_errors = sorted({
            f.title for f in self.findings if f.title in _SCAN_NOT_PERFORMED_TITLES
        })
        image_scan_failed = any(
            t in _IMAGE_SCAN_REQUIRED_TITLES for t in self.tool_errors
        )
        self.passed = (
            not image_scan_failed
            and self.critical_count == 0
            and self.hadolint_error_count == 0
            and self.secret_count == 0
        )
        self.blocked = not self.passed
        return self.passed


# ---------------------------------------------------------------------------
# Q3 SBOM
# ---------------------------------------------------------------------------

class SBOMRecord(BaseModel):
    image:         str
    sbom_path:     Optional[str] = None
    sbom_hash:     Optional[str] = None
    package_count: int = 0
    generated_at:  str = Field(default_factory=lambda: __import__('datetime').datetime.utcnow().isoformat())
    error:         Optional[str] = None


# ---------------------------------------------------------------------------
# Q3 ECS Deploy
# ---------------------------------------------------------------------------

class ECSDeployStatus(str, Enum):
    PENDING                    = "pending"
    RUNNING                    = "running"
    IN_PROGRESS                = "in_progress"
    SUCCEEDED                  = "succeeded"
    FAILED                     = "failed"
    CANCELLED                  = "cancelled"
    ROLLED_BACK                = "rolled_back"
    CIRCUIT_BREAKER_TRIGGERED  = "circuit_breaker_triggered"


def _default_ecs_region() -> str:
    """ECS 배포 기본 리전.

    AWS_REGION / AWS_DEFAULT_REGION 을 순서대로 보고, 없으면 us-east-1.
    us-east-1 인 이유는 개발·검증 환경(AWS Academy Learner Lab)이
    us-east-1 과 us-west-2 만 허용하기 때문이다.
    """
    import os as _os

    for key in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        value = (_os.environ.get(key) or "").strip()
        if value:
            return value
    return "us-east-1"


class ECSDeployRequest(BaseModel):
    """
    Q3 ECS Rolling Update 요청. ecs_agent.deploy() 의 입력.

    설계서 §Q3-A 의 모든 단계 옵션(run_preflight / run_security_scan /
    generate_sbom)과 Task Definition 렌더링에 필요한 모든 필드를 포함.
    """
    project_id:              str
    cluster:                 str
    service:                 str
    #: 이미 ECR 에 있는 이미지를 그대로 쓸 때 지정. `workspace_path` 를
    #: 주면 여기에 빌드 결과가 채워지므로 비워도 된다.
    image:                   str = ""
    #: 기본 리전. 환경변수 AWS_REGION 을 먼저 보고, 없으면 us-east-1.
    #:
    #: 원래 기본값은 ap-northeast-2(서울)였다. 그런데 개발·검증 환경인
    #: AWS Academy Learner Lab 은 us-east-1 / us-west-2 만 허용해서,
    #: 기본값 그대로 배포하면 전 단계가 조용히 거부당한다. 환경변수를
    #: 먼저 보게 해서 서울로 쓰던 쪽도 깨지지 않게 한다.
    region:                  str = Field(default_factory=_default_ecs_region)

    # Task Definition 렌더링용
    task_definition_family:  str = "recoder-task"
    container_name:          str = "app"
    cpu:                     str = "256"        # ECS Fargate vCPU units
    memory:                  str = "512"        # MiB
    health_check_path:       str = "/health"
    container_port:          int = Field(default=8000, ge=1, le=65535)
    #: ECS 컨테이너 헬스체크 명령. **비우면 ECS 가 컨테이너 상태를
    #: 감시하지 않는다** — 프로세스는 살아 있는데 앱이 죽은 경우를
    #: 못 잡고, 롤백·서킷 브레이커도 걸리지 않는다.
    #:
    #: 기본값을 두지 않는 이유: 이미지마다 쓸 수 있는 명령이 다르다.
    #: curl 을 박아뒀다가 런타임 이미지에 curl 이 없어 컨테이너가 무한
    #: 재시작한 적이 있다. 이미지에 확실히 있는 명령을 호출자가 정한다.
    #: 파이썬 이미지는 `python_http_health_check()` 헬퍼를 쓰면 된다.
    health_check_command:    Optional[list[str]] = None
    env_vars:                dict[str, str] = Field(default_factory=dict)

    # ── 빌드 · 업로드 (FR-05-04) ────────────────────────────────────────
    #: 이미지를 빌드할 로컬 작업 폴더. 비우면 빌드를 건너뛰고 `image` 를 쓴다.
    workspace_path:          Optional[str] = None
    dockerfile:              str = "Dockerfile"
    #: 올릴 ECR 리포지토리 이름. 비우면 service 이름을 쓴다.
    ecr_repo:                Optional[str] = None
    #: 이미지 태그. 비우면 배포 시각으로 만든다.
    image_tag:               Optional[str] = None

    # ── 인프라 확보 (FR-05-04) ──────────────────────────────────────────
    #: 참이면 클러스터·로그그룹·ECR·보안그룹·서비스를 없을 때 만들어 준다.
    #: 이미 있으면 그대로 재사용한다(멱등).
    provision:               bool = True
    #: 띄울 태스크 수. 0 이면 서비스만 만들고 태스크는 띄우지 않는다
    #: (Fargate 는 실행 중인 태스크에만 과금되므로 이 상태는 0원).
    desired_count:           int = Field(default=1, ge=0, le=10)
    #: 사용할 서브넷. 비우면 기본 VPC 에서 자동으로 찾는다.
    subnet_ids:              list[str] = Field(default_factory=list)
    #: 사용할 보안 그룹. 비우면 앱 포트를 여는 그룹을 만들어 쓴다.
    security_group_ids:      list[str] = Field(default_factory=list)

    # 파이프라인 옵션
    #: 어느 브랜치에서 배포하는가. **정책 평가에만 쓴다.**
    #: 프리셋 규칙 중 "프로덕션은 main 에서만"이 이 값을 본다. 비어 있으면
    #: 그 규칙이 **아무것도 막지 못한다** — 값을 안 넘기면 규칙이 있으나
    #: 마나가 된다.
    branch:                  str = ""
    #: 배포 대상 환경(staging / production 등). 위 규칙의 다른 한 축이다.
    environment:             str = ""

    run_preflight:           bool = True
    run_security_scan:       bool = True
    generate_sbom:           bool = True
    #: 태스크 공인 IP 를 기다릴 최대 시간(초). 0 이면 URL 확인을 건너뛴다.
    url_wait_timeout:        float = Field(default=300.0, ge=0)

    # 정책
    approval_level:          int = Field(default=3, ge=1, le=4)


class ECSDeployRecord(BaseModel):
    """
    Q3 ECS Rolling Update 결과. ecs_agent 가 단계별로 채워나간다.

    설계서 §Q3-A "Health Check 실패 시 이전 Task Definition 으로
    rollback proposal 생성 (Approval Level 3)" 의 모든 흔적을 보존한다.
    """
    deployment_id:                  str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id:                     Optional[str] = None
    cluster:                        Optional[str] = None
    service:                        Optional[str] = None
    region:                         Optional[str] = None
    image:                          Optional[str] = None
    status:                         ECSDeployStatus = ECSDeployStatus.PENDING
    request:                        Optional[ECSDeployRequest] = None

    # 단계 결과
    preflight_passed:               bool = False
    scan_result:                    Optional[SecurityScanResult] = None
    sbom:                           Optional[SBOMRecord] = None
    sbom_path:                      Optional[str] = None
    sbom_version:                   Optional[str] = None

    # Task Definition 추적 (rollback 대상)
    task_definition_arn:            Optional[str] = None
    previous_task_definition_arn:   Optional[str] = None

    # ── FR-05-04 결과 ───────────────────────────────────────────────────
    #: 카드 DoD 1번 "URL 로 접속됨". 배포가 성공하면 여기에 주소가 담긴다.
    service_url:                    Optional[str] = None
    #: ECR 에 올라간 최종 이미지 주소 (repositoryUri:tag).
    image_uri:                      Optional[str] = None
    image_digest:                   Optional[str] = None
    #: 이번 실행에서 확보한 리소스들 — 무엇이 새로 생겼고 무엇을 재사용했는지.
    #:
    #: **값은 문자열만 넣는다.** 파이단틱은 dict 에 in-place 로 넣는 값까지
    #: 검사하지 않으므로, list 를 넣어도 쓰는 순간에는 아무 일도 안 일어난다.
    #: 대신 `_load_records()` 가 다시 읽을 때 ValidationError 가 나서 그
    #: **기록 한 건이 통째로 버려진다.** 진행 중이던 배포가 그렇게 사라지면
    #: 비용 경고도 클러스터·서비스 이름도 함께 없어져서, 떠 있는 태스크를
    #: 제품 안에서 멈출 방법이 사라진다 — `_save_records` 가 막으려던 바로
    #: 그 상황이다. 구조화된 값이 필요하면 아래 `scan_gaps` 처럼 **전용
    #: 필드**를 만든다.
    provisioned:                    dict[str, str] = Field(default_factory=dict)
    #: 실행하지 못한 보안 검사의 사유 목록. `provisioned["scan_warning"]`
    #: 문구를 **중복 없이 다시 렌더하기 위한 재료**다 (`_add_scan_gaps`).
    scan_gaps:                      list[str] = Field(default_factory=list)
    #: 실패했을 때 사용자가 할 수 있는 일 (DoD 3번).
    error_remedy:                   Optional[str] = None
    #: 실패 원인의 AWS 원문. 사람용 메시지와 분리해서 보존한다.
    error_detail:                   Optional[str] = None

    # 폴링 / Circuit Breaker
    health_check_failures:          int = 0
    circuit_breaker_triggered:      bool = False
    #: 마지막 폴링에서 실제로 떠 있던 태스크 수. 실패 처리에서 "아무것도
    #: 못 뜬 상태"와 "뜨긴 떴는데 느린 상태"를 가르는 데 쓴다.
    running_task_count:             int = 0
    #: 사용자가 취소를 요청했는가. **상태값이 아니라 신호다.**
    #: 취소 엔드포인트가 상태를 바로 FAILED 로 바꿔버리면 두 가지가 망가진다:
    #: (1) 파이프라인은 그걸 모르고 계속 돌아 태스크를 띄운 뒤 SUCCEEDED 로
    #:     덮어쓰고, (2) 409 동시 배포 가드가 그 배포를 "끝난 것"으로 보게 되어
    #:     같은 서비스에 두 번째 파이프라인이 들어온다.
    #: 그래서 신호만 남기고, 상태는 파이프라인이 실제로 멈춘 뒤에 바꾼다.
    cancel_requested:               bool = False
    #: 이번 실행에서 서비스를 **새로 만들었는가**. 취소·실패 때 태스크를
    #: 내려도 되는지 판단한다 — 원래 돌던 서비스를 갱신한 경우라면
    #: 내리는 순간 사용자가 요청하지도 않은 장애가 된다.
    service_created_by_this_run:    bool = False
    #: ECS 서킷 브레이커가 우리 리비전을 버리고 이전 리비전으로 되돌렸는가.
    #: 이 경우 서비스는 **동작하지만 우리가 올린 이미지가 아니다** —
    #: 성공으로 보고하면 사용자는 배포되지 않은 코드를 배포됐다고 믿는다.
    ecs_rolled_back:                bool = False

    # Rollback proposal (설계서 §Q3-A Approval Level 3)
    rollback_proposal_id:           Optional[str] = None
    rollback_approval_level:        Optional[int] = None
    #: pending | approving | completed | ignored | failed. 제안과 실제 실행을
    #: 분리해서, 승인 버튼을 누르기 전에는 ECS 를 절대 건드리지 않는다.
    rollback_proposal_status:       Optional[str] = None
    #: 승인/무시 결과를 포함한 감사 로그. `provisioned` 는 문자열만 허용하므로
    #: 구조화된 상태는 전용 필드로 보존한다.
    rollback_completed_at:           Optional[datetime] = None

    # Lifecycle
    error_message:                  Optional[str] = None
    started_at:                     datetime = Field(default_factory=lambda: datetime.now(timezone.utc) if 'timezone' in globals() else datetime.utcnow())
    completed_at:                   Optional[datetime] = None


# forward reference 해소
SecurityScanResult.model_rebuild()
ECSDeployRecord.model_rebuild()


# ===========================================================================
# v10 PART II/III/IV — 시장성 강화 + 멀티 채널 + Hybrid Cloud Relay
#
# 설계서 v10.0 §29~46 의 모든 데이터 계약을 한 곳에 정리.
# 4명 팀원 (A/B/C/D) 모두가 이 스키마를 공통 인터페이스로 사용하므로
# 변경 시 합의 필수. 추가 시 본 영역 끝의 model_rebuild() 블록에도 등록.
#
# 보안 / 마스킹 정책:
#   - "preview" / "error_message" / "logs_excerpt" 같은 자유 텍스트 필드는
#     ContextGate.mask()를 통과한 마스킹된 값이 들어가야 한다.
#   - 호출자가 마스킹 책임을 진다. 본 모델은 형태만 정의.
#   - SSH key / API key / secret 원문 필드는 본 모델에 정의하지 않는다.
# ===========================================================================


# ── §29 Release Contract ───────────────────────────────────────────────

class ContractStack(str, Enum):
    """프로젝트 스택 — Release Contract.project.stack 의 허용 값."""
    PYTHON_FASTAPI = "python-fastapi"
    PYTHON_FLASK   = "python-flask"
    NODE_EXPRESS   = "node-express"
    NODE_NEXT      = "node-next"
    CUSTOM         = "custom"


class ContractProjectMeta(BaseModel):
    """recoder.yml 의 project 섹션 — 명시적 모델 (§29.2)."""
    stack:           ContractStack
    name:            Optional[str] = None
    package_manager: Optional[str] = None        # "pip" | "npm" | "yarn" | "pnpm"


class ContractRuntime(BaseModel):
    """recoder.yml의 runtime 섹션 — 컨테이너 실행 파라미터 (§29.2, §29.3)."""
    app_port:           int = Field(8000, ge=1, le=65535, description="컨테이너 내부 포트")
    host_port:          int = Field(8000, ge=1, le=65535, description="로컬 노출 포트")
    health_check_path:  str = Field("/health", min_length=1)
    env_file:           str = Field(".env", min_length=1)

    @field_validator("health_check_path")
    @classmethod
    def _validate_health_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("health_check_path must start with '/'")
        # 명령 주입 방지 — / 외 위험 문자 차단
        if any(c in v for c in (" ", ";", "&", "|", "<", ">", "`", "$", "(", ")")):
            raise ValueError(f"health_check_path contains forbidden characters: {v!r}")
        return v

    @field_validator("env_file")
    @classmethod
    def _validate_env_file(cls, v: str) -> str:
        # 절대경로 차단 — 항상 프로젝트 루트 기준 상대경로
        if v.startswith("/") or (len(v) > 1 and v[1] == ":"):
            raise ValueError(f"env_file must be a relative path, got: {v!r}")
        if ".." in v.split("/") or ".." in v.split("\\"):
            raise ValueError(f"env_file must not contain '..': {v!r}")
        return v


class ContractPreflightPolicy(BaseModel):
    """recoder.yml의 preflight 섹션 — Static/Runtime Preflight 차단 정책 (§29.2)."""
    required_env:                list[str] = Field(default_factory=lambda: ["PORT"])
    block_on_build_fail:         bool = True
    block_on_health_fail:        bool = True
    block_on_port_conflict:      bool = True
    block_on_critical_vuln:      bool = True


class ContractStartupPolicy(BaseModel):
    """operational_policy.startup — 시작 시점 로그 패턴 검증 (§29.2)."""
    timeout:                 str = "30s"
    expected_log_pattern:    str = "Application startup complete"
    forbidden_log_pattern:   str = "ERROR|CRITICAL|Traceback"


class ContractDatabaseDep(BaseModel):
    """단일 외부 의존성 (DB 등) — operational_policy.dependencies.database."""
    url_env:                str = "DATABASE_URL"
    required_at_startup:    bool = True


class ContractDependencyPolicy(BaseModel):
    """operational_policy.dependencies — 외부 의존성 (DB 등) 검증 (§29.2)."""
    database: Optional[ContractDatabaseDep] = None


class ContractSmokeTest(BaseModel):
    """operational_policy.smoke_tests[] — 배포 직후 호출 검증 (§29.2)."""
    path:             str
    method:           Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"] = "GET"
    expected_status:  list[int] = Field(default_factory=lambda: [200])

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("smoke test path must start with '/'")
        return v


class ContractResourceLimits(BaseModel):
    """operational_policy.resource_limits (§29.2)."""
    memory: str = Field("512m", pattern=r"^\d+[kKmMgG]?$")
    cpu:    float = Field(0.5, gt=0.0, le=64.0)


class ContractContinuousVerification(BaseModel):
    """operational_policy.continuous_verification — 배포 후 5분 감시 (§29.2, §34)."""
    duration:                 str = "5m"
    health_check_interval:    str = "30s"
    error_log_threshold:      str = "10/min"


class ContractAutoRollbackTrigger(BaseModel):
    """auto_rollback_on 의 단위 트리거 — 명시적 모델."""
    health_check_fail_count:  Optional[int] = None
    error_log_rate_exceeded:  Optional[bool] = None
    memory_usage_exceeded:    Optional[float] = None    # 0~1 (90% = 0.9)


class ContractRollbackStrategy(BaseModel):
    """operational_policy.rollback_strategy (§29.2)."""
    type:                Literal["previous_image", "blue_green", "manual"] = "previous_image"
    auto_rollback_on:    list[ContractAutoRollbackTrigger] = Field(default_factory=list)


class ContractOperationalPolicy(BaseModel):
    """operational_policy 섹션 전체 (§29.2)."""
    startup:                  ContractStartupPolicy = Field(default_factory=ContractStartupPolicy)
    dependencies:             ContractDependencyPolicy = Field(default_factory=ContractDependencyPolicy)
    smoke_tests:              list[ContractSmokeTest] = Field(default_factory=list)
    resource_limits:          ContractResourceLimits = Field(default_factory=ContractResourceLimits)
    continuous_verification:  ContractContinuousVerification = Field(default_factory=ContractContinuousVerification)
    rollback_strategy:        ContractRollbackStrategy = Field(default_factory=ContractRollbackStrategy)


class ReleaseContract(BaseModel):
    """
    설계서 §29 Release Contract — recoder.yml 의 Pydantic 표현.

    First Run Wizard (§36) 가 프로젝트 스캔 + 5개 질문으로 자동 생성하며,
    이후 모든 Preflight/RemediationProposal/CommandTemplate의 기준점이 된다.

    contract_hash 는 본 모델 직렬화의 SHA256 — DeploymentLedger.contract_hash 와
    매칭되어 "이 배포가 어떤 contract 버전으로 검증됐는지" 추적된다 (§29.4).
    """
    project:              ContractProjectMeta
    runtime:              ContractRuntime = Field(default_factory=ContractRuntime)
    preflight:            ContractPreflightPolicy = Field(default_factory=ContractPreflightPolicy)
    operational_policy:   ContractOperationalPolicy = Field(default_factory=ContractOperationalPolicy)

    # 추적용 메타
    contract_hash:        Optional[str] = None  # SHA256 (DeploymentLedger 연결)
    created_at:           datetime = Field(default_factory=datetime.utcnow)
    updated_at:           datetime = Field(default_factory=datetime.utcnow)

    @model_validator(mode="after")
    def _validate_ports(self) -> "ReleaseContract":
        # host_port == app_port 일 때는 매핑이 무의미하지 않지만, 외부 호환을 위해 허용.
        # 다만 둘 다 well-known reserved 포트(<1024)는 권장하지 않음 — 경고 차원에서 거부하지 않음.
        return self


# ── §30 Static Preflight ───────────────────────────────────────────────

class PreflightStatus(str, Enum):
    """Preflight 최종 상태 (§30.2). 제품 로직은 status 기준 동작."""
    BLOCKED  = "BLOCKED"
    WARN     = "WARN"
    PASSED   = "PASSED"


class PreflightCheckCode(str, Enum):
    """Static Preflight 12종 검사 코드 (§30.1)."""
    # 환경 / 설정
    MISSING_REQUIRED_ENV       = "MISSING_REQUIRED_ENV"
    ENV_FILE_NOT_GITIGNORED    = "ENV_FILE_NOT_GITIGNORED"
    INVALID_ENV_FORMAT         = "INVALID_ENV_FORMAT"
    # 코드 / 엔드포인트
    MISSING_HEALTH_ENDPOINT    = "MISSING_HEALTH_ENDPOINT"
    APP_ENTRYPOINT_NOT_FOUND   = "APP_ENTRYPOINT_NOT_FOUND"
    # Docker
    MISSING_DOCKERFILE         = "MISSING_DOCKERFILE"
    DOCKERFILE_BUILD_RISK      = "DOCKERFILE_BUILD_RISK"
    # 포트 / 네트워크
    HOST_PORT_CONFLICT         = "HOST_PORT_CONFLICT"
    APP_PORT_MISMATCH          = "APP_PORT_MISMATCH"
    # 의존성 / 보안
    UNPINNED_DEPENDENCIES      = "UNPINNED_DEPENDENCIES"
    CRITICAL_VULNERABILITY     = "CRITICAL_VULNERABILITY"
    SECRET_LEAK_RISK           = "SECRET_LEAK_RISK"


class PreflightSeverity(str, Enum):
    LOW       = "low"
    MEDIUM    = "medium"
    HIGH      = "high"
    CRITICAL  = "critical"


class PreflightBlocker(BaseModel):
    """배포를 차단하는 항목 (§30.2)."""
    code:                    PreflightCheckCode
    message:                 str
    fix_hint:                str = ""
    remediation_available:   bool = False
    proposal_id:             Optional[str] = None  # 생성된 RemediationProposal 참조
    severity:                PreflightSeverity = PreflightSeverity.HIGH


class PreflightWarning(BaseModel):
    """배포는 가능하지만 주의가 필요한 항목 (§30.2)."""
    code:                    PreflightCheckCode
    message:                 str
    fix_hint:                str = ""
    proposal_id:             Optional[str] = None
    severity:                PreflightSeverity = PreflightSeverity.LOW


class PreflightStaticChecks(BaseModel):
    """§30 Static Preflight 의 12종 개별 결과 — 디버깅용 상세.

    results[<code>] = {"passed": bool, "duration_ms": int, "details": dict}
    """
    results: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PreflightRuntimeChecks(BaseModel):
    """§31 Runtime Preflight 의 검증 항목별 결과 — 디버깅용 상세."""
    container_alive:    Optional[bool] = None
    health_passed:      Optional[bool] = None
    smoke_passed:       Optional[bool] = None
    log_pattern_ok:     Optional[bool] = None
    db_connected:       Optional[bool] = None
    temp_container_id:  Optional[str] = None       # 임시 컨테이너 (정리 추적용)
    duration_ms:        Optional[int] = None
    container_log_tail: Optional[str] = None       # 실패 시 마지막 50줄 (마스킹 필수)


class PreflightRun(BaseModel):
    """
    §33.1 PreflightRun — Static + Runtime Preflight 의 통합 결과.
    Layer 1 (배포 전 검사) 의 단위 레코드. SQLite 영속화 대상.
    """
    preflight_run_id:    str = Field(default_factory=lambda: f"pre_{uuid.uuid4().hex[:8]}")
    project_id:          Optional[str] = None
    contract_hash:       Optional[str] = None
    status:              PreflightStatus = PreflightStatus.PASSED
    score:               int = Field(0, ge=0, le=100)
    blockers:            list[PreflightBlocker] = Field(default_factory=list)
    warnings:            list[PreflightWarning] = Field(default_factory=list)
    static_checks:       PreflightStaticChecks = Field(default_factory=PreflightStaticChecks)
    runtime_checks:      PreflightRuntimeChecks = Field(default_factory=PreflightRuntimeChecks)
    proposal_ids:        list[str] = Field(default_factory=list)
    created_at:          datetime = Field(default_factory=datetime.utcnow)


# ── §32 RemediationProposal ────────────────────────────────────────────

class RemediationTargetType(str, Enum):
    """수정 대상 분류 (§32.1)."""
    ENV_FILE          = "env_file"
    SOURCE_CODE       = "source_code"
    RELEASE_CONTRACT  = "release_contract"
    DOCKER_RUNTIME    = "docker_runtime"
    GUIDANCE_ONLY     = "guidance_only"


class RemediationApplyMethod(str, Enum):
    """적용 방식 (§32.1) — 결정론적 동치성을 위해 LLM 직접 생성 금지."""
    FILE_TEMPLATE     = "file_template"      # FileTemplate Registry 사용
    CONTRACT_UPDATE   = "contract_update"    # recoder.yml 갱신
    COMMAND_TEMPLATE  = "command_template"   # CommandTemplate Registry 사용
    MANUAL_ONLY       = "manual_only"        # 자동 적용 불가, 가이드만


class RemediationPreviewType(str, Enum):
    """미리보기 형식 (§32.1)."""
    DIFF          = "diff"
    FILE_CONTENT  = "file_content"
    COMMAND       = "command"
    GUIDANCE      = "guidance"


class RemediationFallback(str, Enum):
    """자동 적용 실패 시 폴백 (§32.1)."""
    MANUAL_GUIDANCE     = "manual_guidance"
    SKIP                = "skip"
    ASK_USER_FOR_PATH   = "ask_user_for_path"


class RemediationPreviewDiff(BaseModel):
    """preview_type=diff 일 때의 본문 — unified diff 한 줄."""
    unified_diff:   str
    base_sha256:    Optional[str] = None       # 적용 전 파일 hash (Code Rollback 안전망)


class RemediationPreviewFile(BaseModel):
    """preview_type=file_content 일 때의 본문."""
    target_path:    str
    content:        str


class RemediationPreviewCommand(BaseModel):
    """preview_type=command 일 때의 본문 — CommandTemplate 렌더링 결과."""
    command:           str
    template_id:       str
    requires_consent:  bool = True


class RemediationPreviewGuidance(BaseModel):
    """preview_type=guidance 일 때의 본문 — 사용자에게 보여줄 단계."""
    steps:           list[str]
    docs_url:        Optional[str] = None
    estimated_time:  Optional[str] = None      # "5분" 등 자연어


class RemediationProposal(BaseModel):
    """
    §32 RemediationProposal — PatchProposal/InfraFileProposal 의 통합 확장.

    Preflight Blocker 에 1:N 대응. 결정론적 동치성 (§32.2):
        Wizard → recoder.yml (값 확정)
            ↓
        Static Preflight → Blocker 감지 → RemediationProposal 생성
            ↓  template_id + template_variables (Contract 값 기반)
        Apply Engine → Template 치환 → 실제 파일 변경
            ↓
        Re-run Preflight → PASSED
    """
    proposal_id:              str = Field(default_factory=lambda: f"rem_{uuid.uuid4().hex[:8]}")
    source_blocker_code:      PreflightCheckCode
    summary:                  str
    rationale:                str                                # LLM 이 생성하는 자연어 설명 (마스킹된 값 입력 책임은 호출자)
    target_type:              RemediationTargetType
    target_path:              Optional[str] = None
    approval_level:           Optional[int] = Field(None, ge=1, le=4)  # ApprovalLevel 1~4
    risk_level:               RiskLevel = RiskLevel.LOW
    apply_method:             RemediationApplyMethod
    template_id:              Optional[str] = None               # File/CommandTemplate Registry 참조
    template_variables:       dict[str, Any] = Field(default_factory=dict)
    preview_type:             RemediationPreviewType

    # preview 는 preview_type 별로 위 4가지 중 하나. Union 처리:
    # - diff       → RemediationPreviewDiff
    # - file_content → RemediationPreviewFile
    # - command    → RemediationPreviewCommand
    # - guidance   → RemediationPreviewGuidance
    preview:                  Optional[dict[str, Any]] = None    # 위 4 모델 중 하나의 model_dump()

    auto_apply_available:     bool = False
    confidence:               float = Field(0.0, ge=0.0, le=1.0)
    fallback:                 Optional[RemediationFallback] = None
    rollback_hint:            str = ""
    requires_rerun_preflight: bool = True
    created_at:               datetime = Field(default_factory=datetime.utcnow)


# ── §33.2 RemediationRun ───────────────────────────────────────────────

class RemediationRun(BaseModel):
    """
    Layer 2 — 수정 실행 이력. 어떤 proposal 을 언제 어떤 결과로 적용했는지.
    """
    remediation_run_id:   str = Field(default_factory=lambda: f"run_{uuid.uuid4().hex[:8]}")
    preflight_run_id:     str
    proposal_id:          str
    applied_files:        list[str] = Field(default_factory=list)
    applied_at:           datetime = Field(default_factory=datetime.utcnow)
    success:              bool = False
    rollback_executed:    bool = False
    error_message:        Optional[str] = None                    # 마스킹된 값만 (호출자 책임)


# ── §33.3 DeploymentLedger ─────────────────────────────────────────────

class ApprovalSnapshot(BaseModel):
    """승인 시점 스냅샷 — DeploymentLedger.approval_snapshot (§33.3)."""
    approved_at:        datetime
    approved_action:    str                                       # "deploy" | "rollback" | "restart" | ...
    displayed_risks:    list[str] = Field(default_factory=list)
    approved_by:        str                                       # "vscode" | "discord:<user_id>" | "cli" | ...


class RollbackCandidate(BaseModel):
    """이전 배포 image_digest 참조 (§33.3, §17)."""
    image_digest:   str
    note:           str = ""


class DeploymentLedgerStatus(str, Enum):
    """DeploymentLedger.status — 상태머신 명시."""
    DEPLOYING    = "deploying"
    STABLE       = "stable"
    FAILED       = "failed"
    ROLLED_BACK  = "rolled_back"


class DeploymentLedger(BaseModel):
    """
    Layer 3 — 최종 배포 결과. PreflightRun + RemediationRun 들을 ID 참조로 묶음.
    설계서 §20.6 DeploymentRecord 를 확장한 wire format. 기존 DeploymentRecord 도 유지.
    """
    deployment_id:        str = Field(default_factory=lambda: f"dep_{uuid.uuid4().hex[:8]}")
    project_id:           Optional[str] = None
    preflight_run_id:     Optional[str] = None
    remediation_run_ids:  list[str] = Field(default_factory=list)

    # 빌드 정보
    git_commit:           Optional[str] = None
    image_tag:            Optional[str] = None
    image_digest:         Optional[str] = None
    dockerfile_hash:      Optional[str] = None
    contract_hash:        Optional[str] = None

    # 결과
    status:               DeploymentLedgerStatus = DeploymentLedgerStatus.DEPLOYING
    health_after:         Optional[Literal["healthy", "unhealthy"]] = None
    approval_level:       Optional[int] = Field(None, ge=1, le=4)
    approval_snapshot:    Optional[ApprovalSnapshot] = None
    rollback_candidate:   Optional[RollbackCandidate] = None
    failure_reason:       Optional[str] = None                    # 마스킹된 값만

    # 메타 (env_hash 는 secret 미포함 — 단순 해시)
    # 권장 키: env_hash, target_host, region, deployer_id
    metadata:             dict[str, Any] = Field(default_factory=dict)
    created_at:           datetime = Field(default_factory=datetime.utcnow)


# ── §34 Continuous Verification 결과 ───────────────────────────────────

class CVResultStatus(str, Enum):
    STABLE             = "stable"
    AUTO_ROLLBACK_PROPOSED = "auto_rollback_proposed"
    WARNING            = "warning"


class CVResult(BaseModel):
    """§34 Continuous Verification 5분 감시 결과 — DeploymentLedger.status 갱신용."""
    deployment_id:         str
    duration_seconds:      int = 300
    health_failure_count:  int = 0
    error_log_rate:        float = 0.0                            # 분당 에러 개수
    max_memory_pct:        float = 0.0                            # 0~1
    status:                CVResultStatus = CVResultStatus.STABLE
    auto_rollback_to:      Optional[str] = None                   # rollback_image_digest
    notes:                 list[str] = Field(default_factory=list)
    finished_at:           datetime = Field(default_factory=datetime.utcnow)


# ── §35 IncidentMemory ─────────────────────────────────────────────────

class IncidentMemoryRecord(BaseModel):
    """
    §35.1 IncidentMemory — 같은 사고 두 번 반복 방지.

    fingerprint 매칭 단계 (v0): 완전 일치만 (LLM 호출 0회 / 비용 0원).
    v1 확장: bge-small ONNX 임베딩 유사도 검색 (P2).
    """
    fingerprint:          str = Field(min_length=8)               # error_type + 마지막 파일 + masked 메시지의 SHA256
    project_id:           Optional[str] = None
    symptom:              str                                       # 사용자 표시용 증상 요약 (마스킹된 값)
    root_cause:           str                                       # 원인 분석
    successful_fix:       str                                       # 성공한 해결책 자연어 요약
    applied_proposal_id:  str                                       # 어떤 RemediationProposal 로 해결
    linked_deployment_id: Optional[str] = None
    success_count:        int = Field(1, ge=1)                      # 재발 시 increment
    last_seen_at:         datetime = Field(default_factory=datetime.utcnow)
    user_consent:         bool = False                              # 학습 동의 (옵트인)


class IncidentMemoryMatch(BaseModel):
    """
    §35.2 IncidentMemory 매칭 결과 — 단일 entry + confidence.

    v0: confidence ∈ {1.0 (exact project match), 0.7 (cross-project fallback)}.
    v1: 임베딩 유사도 (0.0~1.0 연속값) 도입 예정.
    """
    entry:       IncidentMemoryRecord
    confidence:  float = Field(1.0, ge=0.0, le=1.0)
    matched_at:  datetime = Field(default_factory=datetime.utcnow)


# ===========================================================================
# v10 PART III — 멀티 채널 + 신박 차별점
# ===========================================================================


# ── §37 Discord ChatOps ────────────────────────────────────────────────

class DiscordPermissionTier(str, Enum):
    """Discord 사용자별 허용 동작 범위 (§37.7)."""
    READ_ONLY        = "read_only"               # 상태 조회만
    APPROVE_L1_L2    = "approve_l1_l2"           # Level 1~2 자동 승인 가능
    REQUIRE_DESKTOP  = "require_desktop"          # Level 3~4 는 데스크탑 강제


class DiscordIdentity(BaseModel):
    """Discord user_id ↔ ReCoder 사용자 매핑 (§37.7)."""
    discord_user_id:    str                                       # Discord 의 unique snowflake
    recoder_user_id:    str                                       # ReCoder 내부 사용자 ID
    permission_tier:    DiscordPermissionTier = DiscordPermissionTier.READ_ONLY
    enabled:            bool = True
    registered_at:      datetime = Field(default_factory=datetime.utcnow)
    last_used_at:       Optional[datetime] = None


class DiscordCommandName(str, Enum):
    """Discord 슬래시 명령 (§37.3)."""
    STATUS          = "status"
    PREFLIGHT       = "preflight"
    APPLY           = "apply"
    DEPLOY          = "deploy"
    ROLLBACK        = "rollback"
    REPLAY          = "replay"
    CODE            = "code"
    STANDUP_NOW     = "standup_now"


class DiscordCommandRequest(BaseModel):
    """Discord Bot → Local Core 로 들어오는 명령 요청 (§37.4 시나리오)."""
    command:            DiscordCommandName
    discord_user_id:    str
    channel_id:         Optional[str] = None
    project_id:         Optional[str] = None
    payload:            dict[str, Any] = Field(default_factory=dict)
    received_at:        datetime = Field(default_factory=datetime.utcnow)


class DiscordCommandResult(BaseModel):
    """Discord Bot 응답에 사용할 결과 (§37.4)."""
    command:            DiscordCommandName
    success:            bool
    message:            str                                       # 한국어 사용자 표시 문구
    embed_blocks:       list[dict[str, Any]] = Field(default_factory=list)  # Discord embed JSON
    actions:            list[dict[str, str]] = Field(default_factory=list)  # 버튼 정의
    requires_typing_confirm: bool = False                         # Level 3~4 추가 인증


# ── §38 Deploy Replay ──────────────────────────────────────────────────

class ReplayEventType(str, Enum):
    """타임라인 이벤트 분류 (§38.2)."""
    DEPLOY_STARTED       = "deploy_started"
    HEALTH_OK            = "health_ok"
    HEALTH_FAIL          = "health_fail"
    CV_STARTED           = "cv_started"
    METRIC_SPIKE         = "metric_spike"
    LOG_ERROR            = "log_error"
    INCIDENT_DETECTED    = "incident_detected"
    DISCORD_NOTIFIED     = "discord_notified"
    USER_ACTION          = "user_action"
    ROLLBACK_STARTED     = "rollback_started"
    ROLLBACK_COMPLETED   = "rollback_completed"
    CV_PASSED            = "cv_passed"


class ReplayEvent(BaseModel):
    """단일 타임라인 이벤트 — 시점 + 종류 + 컨텍스트."""
    timestamp:          datetime
    event_type:         ReplayEventType
    title:              str                                       # "메모리 사용량 급증 시작"
    description:        Optional[str] = None
    metrics:            dict[str, Any] = Field(default_factory=dict)  # {memory_mb, cpu_pct, ...}
    actor:              Optional[str] = None                      # "vscode" | "discord:<user>" | "system" | "watchdog"
    masked_log_excerpt: Optional[str] = None                      # 마스킹 필수


class ReplayTimeline(BaseModel):
    """
    §38 Deploy Replay — 인시던트 영상 재생용 타임라인.
    DeploymentLedger 1개에 대해 발생한 모든 이벤트의 시간순 정렬.
    """
    deployment_id:      str
    project_id:         Optional[str] = None
    start_at:           datetime
    end_at:             datetime
    events:             list[ReplayEvent] = Field(default_factory=list)
    duration_seconds:   int = 0
    incident_detected:  bool = False
    rollback_executed:  bool = False
    created_at:         datetime = Field(default_factory=datetime.utcnow)


# ── §39 Daily Standup ──────────────────────────────────────────────────

class StandupChannel(str, Enum):
    """Standup 전송 채널 (§39.4)."""
    DISCORD_DM       = "discord_dm"
    DISCORD_CHANNEL  = "discord_channel"
    EMAIL            = "email"


class NotableObservation(BaseModel):
    """주목할 점 — Daily Standup 한 항목."""
    category:    Literal["performance", "reliability", "security", "cost"] = "performance"
    message:     str
    severity:    PreflightSeverity = PreflightSeverity.LOW


class RecommendedTask(BaseModel):
    """오늘 추천 작업 — Daily Standup 한 항목."""
    title:       str
    rationale:   str
    estimated_time: Optional[str] = None       # "30분" 등


class StandupStats(BaseModel):
    """주간 / 누적 통계 — Daily Standup."""
    deployments_total:    int = 0
    success_rate:         float = Field(0.0, ge=0.0, le=1.0)
    avg_preflight_score:  float = Field(0.0, ge=0.0, le=100.0)
    incidents_count:      int = 0


class StandupBriefing(BaseModel):
    """
    §39 Daily Standup — 매일 아침 운영 브리핑.
    LLM(Haiku)이 PreflightRun + DeploymentLedger + IncidentMemory 종합 생성.
    """
    briefing_id:        str = Field(default_factory=lambda: f"brf_{uuid.uuid4().hex[:8]}")
    date:               datetime
    project_id:         Optional[str] = None
    channel:            StandupChannel = StandupChannel.DISCORD_DM

    # 어제 / 지난 24h 요약
    yesterday_deployments:  int = 0
    yesterday_incidents:    int = 0
    yesterday_blocks:       int = 0
    notable:                list[NotableObservation] = Field(default_factory=list)
    recommended:            list[RecommendedTask] = Field(default_factory=list)
    weekly_stats:           StandupStats = Field(default_factory=StandupStats)

    # 메타
    delivered_at:       Optional[datetime] = None
    user_disabled:      bool = False
    created_at:         datetime = Field(default_factory=datetime.utcnow)


# ── §41 Deploy Forecast ────────────────────────────────────────────────

class RiskFactor(BaseModel):
    """위험 요인 한 항목 — Deploy Forecast 의 근거."""
    code:        str                            # "RECENT_OOM", "WEEKEND_DEPLOY" 등
    description: str
    weight:      float = Field(0.0, ge=0.0, le=1.0)


class DeployForecastSentiment(str, Enum):
    SUNNY        = "sunny"        # 안정 (위험도 < 25%)
    PARTLY_CLOUDY = "partly_cloudy"  # 주의 (25~50%)
    CLOUDY       = "cloudy"       # 경계 (50~75%)
    STORMY       = "stormy"       # 위험 (>75%)


class DeployForecast(BaseModel):
    """
    §41 Deploy Forecast — 배포 일기예보.
    DeploymentLedger 통계 + IncidentMemory 패턴 매칭으로 위험도 계산.
    """
    forecast_id:        str = Field(default_factory=lambda: f"fc_{uuid.uuid4().hex[:8]}")
    project_id:         Optional[str] = None
    date:               datetime
    sentiment:          DeployForecastSentiment
    risk_percentage:    float = Field(0.0, ge=0.0, le=1.0)
    factors:            list[RiskFactor] = Field(default_factory=list)
    recommendations:    list[str] = Field(default_factory=list)
    created_at:         datetime = Field(default_factory=datetime.utcnow)


# ── §42 Visual Diff ────────────────────────────────────────────────────

class DiffChangeType(str, Enum):
    ADDED     = "added"
    REMOVED   = "removed"
    MODIFIED  = "modified"
    UNCHANGED = "unchanged"


class DiffEntry(BaseModel):
    """배포 전후 비교의 한 항목."""
    field:          str                            # "memory_limit", "endpoints", "image_digest" 등
    change_type:    DiffChangeType
    before:         Optional[Any] = None
    after:          Optional[Any] = None
    note:           Optional[str] = None


class DeploymentDiff(BaseModel):
    """
    §42 Visual Diff — 두 DeploymentLedger 비교 결과.
    프론트가 그래픽 다이어그램으로 렌더링.
    """
    diff_id:            str = Field(default_factory=lambda: f"diff_{uuid.uuid4().hex[:8]}")
    before_deployment_id:   str
    after_deployment_id:    str
    entries:            list[DiffEntry] = Field(default_factory=list)
    created_at:         datetime = Field(default_factory=datetime.utcnow)


# ===========================================================================
# v10 PART IV — Hybrid Cloud Relay (§46)
# ===========================================================================


class CloudRelayUserMapping(BaseModel):
    """
    §46.5 데이터 분리 — Cloud Relay 가 보유하는 유일한 사용자 정보.
    Discord user_id ↔ ReCoder 사용자 ID 매핑 + 권한 토글.
    """
    discord_user_id:        str
    recoder_user_id:        str
    enable_command_queue:   bool = False                          # 흐름 ①
    enable_incident_relay:  bool = False                          # 흐름 ②
    enable_emergency_rollback: bool = False                       # 흐름 ③ (P2 옵트인)
    ssh_key_secret_arn:     Optional[str] = None                  # Secrets Manager ARN (key 본문 미저장)
    ssh_key_expires_at:     Optional[datetime] = None
    created_at:             datetime = Field(default_factory=datetime.utcnow)


class CommandQueueStatus(str, Enum):
    PENDING    = "pending"
    DELIVERED  = "delivered"
    EXECUTED   = "executed"
    FAILED     = "failed"
    EXPIRED    = "expired"


class CommandQueueEntry(BaseModel):
    """
    §46.3.1 명령 큐 — PC 꺼진 상태에서 들어온 명령 보관.
    DynamoDB 에 저장. PC 켜지면 Local Core 가 polling 해서 비움.
    """
    queue_entry_id:     str = Field(default_factory=lambda: f"q_{uuid.uuid4().hex[:8]}")
    recoder_user_id:    str
    discord_user_id:    str
    command_name:       DiscordCommandName
    payload:            dict[str, Any] = Field(default_factory=dict)
    status:             CommandQueueStatus = CommandQueueStatus.PENDING
    enqueued_at:        datetime = Field(default_factory=datetime.utcnow)
    delivered_at:       Optional[datetime] = None
    executed_at:        Optional[datetime] = None
    result_summary:     Optional[str] = None                      # 마스킹된 값만
    expires_at:         Optional[datetime] = None                 # 기본 24h 후


class IncidentNotificationSource(str, Enum):
    WATCHDOG       = "watchdog"
    LOCAL_CORE     = "local_core"
    CONTINUOUS_VERIFICATION = "continuous_verification"


class IncidentNotification(BaseModel):
    """
    §46.3.2 인시던트 클라우드 알림 — Watchdog/CV → Cloud Relay → Discord 즉시 push.
    """
    notification_id:        str = Field(default_factory=lambda: f"n_{uuid.uuid4().hex[:8]}")
    source:                 IncidentNotificationSource
    project_id:             Optional[str] = None
    recoder_user_id:        str
    deployment_id:          Optional[str] = None                  # 연결된 DeploymentLedger
    fingerprint:            Optional[str] = None                  # IncidentMemory 매칭
    symptom:                str                                    # "fastapi-demo 컨테이너 다운"
    severity:               PreflightSeverity = PreflightSeverity.HIGH
    recent_deployment_id:   Optional[str] = None
    masked_log_excerpt:     Optional[str] = None
    delivered_channels:     list[StandupChannel] = Field(default_factory=list)
    detected_at:            datetime = Field(default_factory=datetime.utcnow)


class EmergencyRollbackRequest(BaseModel):
    """
    §46.3.3 긴급 롤백 클라우드 실행 — P2 옵트인.
    SSH key 본문은 본 모델에 포함 안 됨 (CloudRelayUserMapping.ssh_key_secret_arn 으로만 참조).
    """
    request_id:             str = Field(default_factory=lambda: f"erb_{uuid.uuid4().hex[:8]}")
    discord_user_id:        str
    recoder_user_id:        str
    project_id:             Optional[str] = None
    deployment_id:          str                                    # 어느 배포에서 rollback?
    target_deployment_id:   str                                    # 어느 이전 배포로 복구?
    typing_confirm:         str                                    # "rollback dep_041" 사용자가 직접 타이핑
    requested_at:           datetime = Field(default_factory=datetime.utcnow)
    executed:               bool = False
    result_summary:         Optional[str] = None                   # 마스킹된 값만


# ===========================================================================
# v10 — 공통 응답 / 요청 모델 (모든 라우트 통일)
# ===========================================================================


class HealthCheckResponse(BaseModel):
    """GET /api/health 응답 — 인증 면제 엔드포인트."""
    status:           Literal["ok", "degraded", "down"] = "ok"
    version:          str
    uptime_seconds:   float = Field(0.0, ge=0.0)
    port:             int = Field(17894, ge=1, le=65535)


class PaginationCursor(BaseModel):
    """리스트 엔드포인트의 페이지네이션 (cursor 기반).

    클라이언트는 응답의 next_cursor 를 다음 요청의 ?cursor= 파라미터로 전달.
    """
    limit:        int = Field(20, ge=1, le=200, description="최대 200")
    cursor:       Optional[str] = None         # opaque base64 — 서버가 정의
    sort_order:   Literal["asc", "desc"] = "desc"


class PaginatedResponse(BaseModel):
    """리스트 응답 공통 wrapper.

    items 는 호출자가 구체 타입으로 model_dump 한 list 를 넣는다 (Generic 회피).
    """
    items:        list[dict[str, Any]] = Field(default_factory=list)
    total:        Optional[int] = None         # 정확한 카운트가 비싸면 None
    next_cursor:  Optional[str] = None         # None 이면 마지막 페이지
    has_more:     bool = False


class AuditAction(str, Enum):
    """감사 로그 액션 분류 — 모든 부수효과 있는 작업은 기록."""
    PATCH_APPROVED              = "patch_approved"
    PATCH_REJECTED              = "patch_rejected"
    REMEDIATION_APPLIED         = "remediation_applied"
    REMEDIATION_REJECTED        = "remediation_rejected"
    DEPLOYMENT_STARTED          = "deployment_started"
    DEPLOYMENT_SUCCEEDED        = "deployment_succeeded"
    DEPLOYMENT_FAILED           = "deployment_failed"
    ROLLBACK_TRIGGERED          = "rollback_triggered"
    ROLLBACK_COMPLETED          = "rollback_completed"
    DISCORD_IDENTITY_REGISTERED = "discord_identity_registered"
    DISCORD_IDENTITY_REVOKED    = "discord_identity_revoked"
    CLOUD_RELAY_ENABLED         = "cloud_relay_enabled"
    CLOUD_RELAY_DISABLED        = "cloud_relay_disabled"
    EMERGENCY_ROLLBACK_REQUESTED = "emergency_rollback_requested"
    EMERGENCY_ROLLBACK_EXECUTED  = "emergency_rollback_executed"
    SSH_KEY_REGISTERED          = "ssh_key_registered"
    SSH_KEY_ROTATED             = "ssh_key_rotated"
    SESSION_TOKEN_ROTATED       = "session_token_rotated"
    DATA_DELETED                = "data_deleted"


class AuditLogEntry(BaseModel):
    """단일 감사 로그 — 부수효과 작업의 영구 기록 (settings.py § 21.3 보관 기간)."""
    audit_id:        str = Field(default_factory=lambda: f"audit_{uuid.uuid4().hex[:12]}")
    action:          AuditAction
    actor:           str                                  # "vscode" | "discord:<user>" | "system" | "cloud-relay"
    target_type:     str                                  # "deployment" | "remediation" | "patch" | ...
    target_id:       Optional[str] = None
    details:         dict[str, Any] = Field(default_factory=dict)
    request_id:      Optional[str] = None
    occurred_at:     datetime = Field(default_factory=utc_now)
    masked:          bool = True                          # raw 데이터는 보관하지 않음


class TokenRotation(BaseModel):
    """X-Session-Token 회전 응답.

    Core 가 토큰 재발급 시 새 토큰을 반환. Extension/Discord Bot 은 갱신 필요.
    """
    new_token:       str
    rotated_at:      datetime = Field(default_factory=utc_now)
    previous_valid_until: Optional[datetime] = None       # grace period (보통 60초)


class RateLimitInfo(BaseModel):
    """rate limit 응답 헤더 정보를 body 에도 포함하는 경우 (429 에러).

    헤더로는 항상:
        X-RateLimit-Limit: <int>
        X-RateLimit-Remaining: <int>
        X-RateLimit-Reset: <unix-ts>
    """
    limit:           int
    remaining:       int
    reset_at:        datetime
    retry_after_sec: int = Field(0, ge=0)


class ErrorCode(str, Enum):
    """모든 HTTP 4xx/5xx 응답의 공통 에러 코드.

    - 4xx: 사용자 / 입력 오류
    - 5xx: 서버 / 외부 의존성 오류
    """
    # 4xx — 클라이언트 측
    INVALID_REQUEST          = "INVALID_REQUEST"          # 400
    UNAUTHORIZED             = "UNAUTHORIZED"             # 401
    FORBIDDEN                = "FORBIDDEN"                # 403
    NOT_FOUND                = "NOT_FOUND"                # 404
    CONFLICT                 = "CONFLICT"                 # 409 (예: base_sha256 mismatch)
    UNPROCESSABLE            = "UNPROCESSABLE"            # 422 (Pydantic validation 실패)
    TOO_MANY_REQUESTS        = "TOO_MANY_REQUESTS"        # 429
    # 5xx — 서버 측
    INTERNAL_ERROR           = "INTERNAL_ERROR"           # 500 (일반)
    LLM_PROVIDER_ERROR       = "LLM_PROVIDER_ERROR"       # 502 (Bedrock/Gemini 실패)
    DOCKER_ENGINE_ERROR      = "DOCKER_ENGINE_ERROR"      # 502 (Docker 미설치/실패)
    AWS_API_ERROR            = "AWS_API_ERROR"            # 502 (EC2/ECR/Secrets 실패)
    SERVICE_UNAVAILABLE      = "SERVICE_UNAVAILABLE"      # 503 (초기화 미완료)
    # 도메인 별
    PREFLIGHT_FAILED         = "PREFLIGHT_FAILED"         # 422 (Preflight blocker로 거부)
    REMEDIATION_FAILED       = "REMEDIATION_FAILED"       # 500 (Remediation 적용 실패)
    ROLLBACK_INFEASIBLE      = "ROLLBACK_INFEASIBLE"      # 409 (snapshot 없음 등)
    APPROVAL_REQUIRED        = "APPROVAL_REQUIRED"        # 403 (Level 3~4 미승인)


class ErrorResponse(BaseModel):
    """
    모든 라우트의 4xx/5xx 응답 공통 포맷.

    원칙:
      - message 는 사용자에게 보여줄 한 줄 (마스킹된 값만)
      - detail 은 디버깅용 (서버 로그에는 traceback, 응답에는 마스킹된 요약)
      - request_id 는 추적용 (모든 응답에 X-Request-ID 헤더와 동일 값)
      - timestamp 는 ISO8601 UTC
    """
    error:        bool = True
    code:         ErrorCode
    message:      str
    detail:       Optional[str] = None                    # 마스킹된 값만
    request_id:   Optional[str] = None
    timestamp:    datetime = Field(default_factory=datetime.utcnow)


# ── Request / Response — Preflight ─────────────────────────────────────

class RunPreflightRequest(BaseModel):
    """POST /api/preflight/run 요청 본문."""
    project_id:       Optional[str] = None
    contract_path:    str = "recoder.yml"                 # 프로젝트 루트 기준
    include_runtime:  bool = True                          # Runtime Preflight 실행 여부 (B 영역)
    timeout_sec:      int = Field(60, ge=10, le=300)


class ApplyRemediationRequest(BaseModel):
    """POST /api/remediations/{proposal_id}/apply 요청 본문."""
    approve:          bool                                 # false면 거절
    user_consent:     bool = False                         # IncidentMemory 학습 동의 (옵트인)
    typing_confirm:   Optional[str] = None                 # Level 4 필요시


class ApplyRemediationResponse(BaseModel):
    """POST /api/remediations/{proposal_id}/apply 응답 본문."""
    success:          bool
    remediation_run:  Optional[RemediationRun] = None
    rerun_required:   bool = True                          # 적용 후 Preflight 재실행 필요?
    next_action:      Optional[Literal["rerun_preflight", "deploy", "rollback", "none"]] = None


# ── Request / Response — Deployment ────────────────────────────────────

class CreateDeploymentRequest(BaseModel):
    """POST /api/deployments 요청 본문 — 배포 시작."""
    project_id:           Optional[str] = None
    preflight_run_id:     str                              # 통과한 PreflightRun 필수
    image_tag:            str
    contract_hash:        Optional[str] = None
    approval_snapshot:    Optional[ApprovalSnapshot] = None


class RollbackDeploymentRequest(BaseModel):
    """POST /api/deployments/{id}/rollback 요청 본문."""
    target_deployment_id:  Optional[str] = None            # None이면 직전 stable로
    user_consent:          bool = False
    typing_confirm:        str                              # "rollback dep_xxx" 형태 필수


# ── Request / Response — Discord ───────────────────────────────────────

class RegisterDiscordIdentityRequest(BaseModel):
    """POST /api/discord/identities 요청 본문."""
    discord_user_id:    str
    permission_tier:    DiscordPermissionTier = DiscordPermissionTier.READ_ONLY


# ── Request / Response — Cloud Relay ───────────────────────────────────

class ExecuteCommandQueueRequest(BaseModel):
    """POST /api/cloud-relay/queue/{entry_id}/execute 요청 본문 — Local Core가 큐 비울 때."""
    acknowledged_at:    datetime = Field(default_factory=datetime.utcnow)


# ===========================================================================
# v10 PART II/III/IV — forward reference 해소
# ===========================================================================

HealthCheckResponse.model_rebuild()
PaginationCursor.model_rebuild()
PaginatedResponse.model_rebuild()
AuditLogEntry.model_rebuild()
TokenRotation.model_rebuild()
RateLimitInfo.model_rebuild()
ErrorResponse.model_rebuild()
RunPreflightRequest.model_rebuild()
ApplyRemediationRequest.model_rebuild()
ApplyRemediationResponse.model_rebuild()
CreateDeploymentRequest.model_rebuild()
RollbackDeploymentRequest.model_rebuild()
RegisterDiscordIdentityRequest.model_rebuild()
ExecuteCommandQueueRequest.model_rebuild()
ReleaseContract.model_rebuild()
PreflightRun.model_rebuild()
RemediationProposal.model_rebuild()
RemediationRun.model_rebuild()
DeploymentLedger.model_rebuild()
CVResult.model_rebuild()
IncidentMemoryRecord.model_rebuild()
# PART III
DiscordIdentity.model_rebuild()
DiscordCommandRequest.model_rebuild()
DiscordCommandResult.model_rebuild()
ReplayTimeline.model_rebuild()
StandupBriefing.model_rebuild()
DeployForecast.model_rebuild()
DeploymentDiff.model_rebuild()
# PART IV
CloudRelayUserMapping.model_rebuild()
CommandQueueEntry.model_rebuild()
IncidentNotification.model_rebuild()
EmergencyRollbackRequest.model_rebuild()


# ===========================================================================
# Orchestrator FSM Layer — dataclass-based runtime types (설계서 §6)
#
# 이 영역은 Local Core 내부의 FSM·이벤트 디스패치용 가벼운 dataclass 들이다.
# 위쪽 Pydantic 모델이 "wire format" 이라면 이쪽은 "in-process" 표현이며,
# Orchestrator·Context Gate·SessionLogger 가 직접 참조한다.
# ===========================================================================


# ── 공통 열거형 ────────────────────────────────────────────────────────

class ContextSource(str, Enum):
    TERMINAL   = "terminal"
    FILE       = "file"
    USER_INPUT = "user_input"
    WORKSPACE  = "workspace"

class ContextWeight(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"

class EventType(str, Enum):
    ERROR_DETECTED = "error_detected"
    TASK_CHANGE    = "task_change"
    FILE_CHANGED   = "file_changed"
    USER_QUESTION  = "user_question"
    TERMINAL_ERROR = "terminal_error"
    RESOLVED       = "resolved"

class OrchestratorState(str, Enum):
    IDLE                 = "IDLE"
    ERROR_DETECTED       = "ERROR_DETECTED"
    ANALYZING            = "ANALYZING"
    WAITING_USER_ACTION  = "WAITING_USER_ACTION"
    CODE_PATCH_PROPOSED  = "CODE_PATCH_PROPOSED"
    APPLYING_PATCH       = "APPLYING_PATCH"
    CODE_READY           = "CODE_READY"
    SECURITY_SCANNING    = "SECURITY_SCANNING"    # Trivy/Hadolint 실행 중
    INFRA_PROPOSED       = "INFRA_PROPOSED"
    INFRA_READY          = "INFRA_READY"
    DOCKER_BUILDING      = "DOCKER_BUILDING"
    HEALTH_CHECKING      = "HEALTH_CHECKING"
    DEPLOY_PROPOSED      = "DEPLOY_PROPOSED"
    DEPLOYING            = "DEPLOYING"
    DEPLOYED             = "DEPLOYED"
    DEPLOY_FAILED        = "DEPLOY_FAILED"
    ROLLBACK             = "ROLLBACK"

class UserAction(str, Enum):
    FIX_CODE       = "fix_code"
    EXPLAIN        = "explain_error"
    IGNORE         = "ignore"
    INFRA          = "infra"
    DEPLOY         = "deploy"
    LOCAL_DEPLOY   = "local_deploy"
    GIT_COMMIT     = "git_commit"
    DOCKERFILE     = "generate_dockerfile"
    DOCKER_COMPOSE = "generate_docker_compose"
    GITHUB_ACTIONS = "generate_github_actions"
    SCAN_SECURITY  = "scan_security"
    OPS_QUERY      = "ops_query"          # Stage 3

# NOTE: RiskLevel / DeployMethod / DeployStatus 는 위 Pydantic enum 이 canonical.
#       dataclass 측 정의는 제거. ProjectStack / ReadyStatus 는 legacy 코드 호환을
#       위해 alias 로 유지한다.


class ProjectStack(str, Enum):
    """Legacy alias for StackType — 1학기 MVP 4종 + custom."""
    PYTHON_FASTAPI = "python-fastapi"
    PYTHON_FLASK   = "python-flask"
    NODE_EXPRESS   = "node-express"
    NODE_NEXT      = "node-next"
    CUSTOM         = "custom"


class ReadyStatus(str, Enum):
    """Legacy alias used by first_run / orchestrator for diagnostics output."""
    OK      = "ok"
    PARTIAL = "partial"
    WARN    = "warn"
    FAIL    = "fail"


# ── ExtractedContext (Context Gate 출력) ──────────────────────────────

@dataclass
class ExtractedContext:
    context_id:           str
    source:               ContextSource
    app_name:             str
    window_title:         str
    text:                 str
    weight:               ContextWeight
    quality_score:        float
    failure_flag:         bool
    captured_at:          str
    masked_text:          str = ""
    raw_text_memory_only: bool = True
    masking_applied:      bool = True
    masked_patterns:      list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "context_id":           self.context_id,
            "source":               self.source.value,
            "app_name":             self.app_name,
            "window_title":         self.window_title,
            "text":                 self.text,
            "masked_text":          self.masked_text or self.text,
            "raw_text_memory_only": self.raw_text_memory_only,
            "masking_applied":      self.masking_applied,
            "masked_patterns":      self.masked_patterns,
            "weight":               self.weight.value,
            "quality_score":        self.quality_score,
            "failure_flag":         self.failure_flag,
            "captured_at":          self.captured_at,
        }


# ── AgentEvent ───────────────────────────────────────────────────────

@dataclass
class AgentEvent:
    event_id:          str
    event_type:        EventType
    summary:           str
    contexts:          list[str]
    importance_score:  int
    suggested_actions: list[UserAction]
    created_at:        str
    raw_errors:        list[str] = field(default_factory=list)
    error_text:        str = ""
    trigger_score:     int = 0
    trigger_reasons:   list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "event_id":          self.event_id,
            "event_type":        self.event_type.value,
            "summary":           self.summary,
            "contexts":          self.contexts,
            "importance_score":  self.importance_score,
            "suggested_actions": [a.value for a in self.suggested_actions],
            "created_at":        self.created_at,
            "raw_errors":        self.raw_errors,
            "error_text":        self.error_text,
            "trigger_score":     self.trigger_score,
            "trigger_reasons":   self.trigger_reasons,
        }


# NOTE: 아래 영역에서 PatchProposal / InfraFileProposal / DeploymentPlan /
#       DeploymentRecord / AlertRecord / ResponseProposal / CommandTemplate /
#       FileTemplate / LLMCallRecord / SessionRecord / DiagnosticsResult 는
#       모두 위쪽 Pydantic 정의가 canonical 이다. dataclass 복제본은 제거됨.
#       legacy 코드가 schemas.LLMCallRecord 등으로 import 해도 Pydantic 버전을
#       그대로 받게 된다.


# ── LLM Usage Summary (legacy dataclass — Pydantic LLMCallRecord 와 별개) ─

@dataclass
class LLMUsageSummary:
    """세션 단위 LLM 누적 사용량 (legacy logger 가 dict로 직렬화)."""

    total_input_tokens:       int   = 0
    total_output_tokens:      int   = 0
    estimated_total_cost_usd: float = 0.0
    pricing_version:          str   = ""

    def to_dict(self) -> dict:
        return {
            "total_input_tokens":       self.total_input_tokens,
            "total_output_tokens":      self.total_output_tokens,
            "estimated_total_cost_usd": self.estimated_total_cost_usd,
            "pricing_version":          self.pricing_version,
        }


# ── SessionEvent (legacy dataclass — Pydantic SessionRecord.events 에 dict 로 들어감) ─

@dataclass
class SessionEvent:
    """SessionRecord.events 의 단위 entry. SessionLogger 가 dict 변환해서 저장."""

    time:                  str
    event_type:            str
    error_summary:         str
    error_fingerprint:     str
    related_file_names:    list[str]
    ai_suggestion_summary: str
    user_action:           Literal["approved", "rejected", "ignored"]
    result:                Literal["success", "failed", "pending"]
    validation:            Literal["test_passed", "syntax_ok", "unknown"]


# ── Orchestrator 상태 업데이트 (server → Extension push payload) ─────────

@dataclass
class OrchestratorUpdate:
    """
    Local Core → VSCode Extension 으로 폴링/푸시되는 상태 업데이트 페이로드.

    proposal/plan 은 위쪽 Pydantic 모델 (PatchProposal, InfraFileProposal,
    DeploymentPlan) 또는 그것의 dict 직렬화 형태를 받는다. 직렬화는 model_dump()
    또는 .to_dict() 둘 다 지원한다.
    """

    state:          OrchestratorState
    event:          Optional["AgentEvent"]              = None
    patch_proposal: Optional[Any]                       = None  # PatchProposal | dict
    infra_proposal: Optional[Any]                       = None  # InfraFileProposal | dict
    plan:           Optional[Any]                       = None  # DeploymentPlan | dict
    message:        str                                 = ""

    @staticmethod
    def _serialize(obj: Any) -> Optional[dict]:
        if obj is None:
            return None
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        if isinstance(obj, dict):
            return obj
        return None

    def to_dict(self) -> dict:
        return {
            "type":           "orchestrator_update",
            "state":          self.state.value,
            "event":          self.event.to_dict() if self.event else None,
            "patch_proposal": self._serialize(self.patch_proposal),
            "infra_proposal": self._serialize(self.infra_proposal),
            "plan":           self._serialize(self.plan),
            "message":        self.message,
        }
