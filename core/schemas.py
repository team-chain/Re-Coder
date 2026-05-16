"""
ReCoder Core Data Contracts (Section 20)
Pydantic v2 schemas for all inter-component data exchange.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


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
    UNKNOWN = "unknown"


class DeployMethod(str, Enum):
    LOCAL_DOCKER = "local_docker"
    SSH_DOCKER = "ssh_docker"
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


class FileType(str, Enum):
    DOCKERFILE = "dockerfile"
    DOCKER_COMPOSE = "docker_compose"
    GITHUB_ACTIONS = "github_actions"
    NGINX_CONF = "nginx_conf"
    ENV_FILE = "env_file"
    K8S_MANIFEST = "k8s_manifest"
    TERRAFORM = "terraform"


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
    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
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


class FileTemplate(BaseModel):
    """A reusable infrastructure file template."""

    template_id: str
    file_type: FileType
    base_content: str
    customizable_sections: dict[str, str] = Field(default_factory=dict)
    version: str = "1.0"


# ---------------------------------------------------------------------------
# Diagnostic & Runtime Models
# ---------------------------------------------------------------------------


class DiagnosticsResult(BaseModel):
    """Result of the /diagnostics endpoint — system readiness check."""

    core_ready: ReadyState = ReadyState.NOT_READY
    ai_ready: ReadyState = ReadyState.NOT_READY
    docker_ready: ReadyState = ReadyState.NOT_READY
    aws_deploy_ready: ReadyState = ReadyState.NOT_READY
    ops_ready: ReadyState = ReadyState.NOT_READY
    resolved_model_id: Optional[str] = None
    resolved_region: Optional[str] = None
    is_cross_region_profile: bool = False
    provider_type: Optional[ProviderType] = None
    validation_time: datetime = Field(default_factory=datetime.utcnow)
    details: dict[str, Any] = Field(default_factory=dict)


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
