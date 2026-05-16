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


# ---------------------------------------------------------------------------
# Q1 — AST Chunker Models
# ---------------------------------------------------------------------------


class NodeType(str, Enum):
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"
    CLASS = "class"
    MODULE = "module"       # Whole-file fallback
    UNKNOWN = "unknown"


class ChunkMetadata(BaseModel):
    """
    Metadata for a single AST-derived code chunk.

    Security policy: source text is NOT stored here.
    It is re-read from the filesystem on demand, immediately before LLM delivery,
    after passing through ContextGate.
    """

    chunk_id: str                   # SHA-256 of (file_path + name + start_line) — first 8 hex chars
    file_path: str                  # Absolute path on disk
    node_type: NodeType = NodeType.UNKNOWN
    name: str                       # Symbol name (function/class name, or file stem for module)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    token_estimate: int = Field(default=0, ge=0)   # Rough estimate; 0 = uncalculated


# ---------------------------------------------------------------------------
# Q1 — Plan-Execute-Verify Models
# ---------------------------------------------------------------------------


class AgentType(str, Enum):
    CODE_AGENT = "code_agent"
    INFRA_AGENT = "infra_agent"
    DEPLOY_AGENT = "deploy_agent"
    TEST_RUNNER = "test_runner"
    NO_OP = "no_op"


class ExecutionStep(BaseModel):
    """A single step in a PlannerAgent-generated ExecutionPlan."""

    step_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    description: str
    agent: AgentType
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)   # list of step_ids


class ExecutionPlan(BaseModel):
    """
    Structured output produced by PlannerAgent.

    Constraints (enforced by PlannerAgent prompt):
    - Maximum 5 steps.
    - PlannerAgent never executes — it only plans.
    - Executor (deterministic dispatcher) drives actual agent calls.
    """

    schema_version: str = "1.0"
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    summary: str
    steps: list[ExecutionStep] = Field(default_factory=list, max_length=5)
    estimated_risk: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False


class VerificationResult(BaseModel):
    """Result from VerifierAgent (no LLM — deterministic checks only)."""

    plan_id: str
    proposal_id: Optional[str] = None
    schema_valid: bool = False
    sha256_valid: bool = False
    test_passed: Optional[bool] = None    # None = no test_command supplied
    test_output: Optional[str] = None
    retry_count: int = Field(default=0, ge=0)
    needs_manual_review: bool = False     # True when retry limit exhausted
    verified_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Q1 — Eval Harness Models
# ---------------------------------------------------------------------------


class EvalCategory(str, Enum):
    PYTHON_SINGLE_FILE = "python_single_file"
    PYTHON_MULTI_FILE = "python_multi_file"
    NODEJS_ERROR = "nodejs_error"
    DOCKERFILE_GENERATION = "dockerfile_generation"
    DOCKER_BUILD_FAILURE = "docker_build_failure"
    HEALTH_CHECK_FAILURE = "health_check_failure"


class SafetyViolationType(str, Enum):
    SECRET_LEAK = "secret_leak"
    NONEXISTENT_IMPORT = "nonexistent_import"
    INVALID_SHELL_COMMAND = "invalid_shell_command"
    ROLLBACK_NOT_DISCLOSED = "rollback_not_disclosed"
    DESTRUCTIVE_OPERATION = "destructive_operation"


class EvalCase(BaseModel):
    """A single evaluation test case for the Eval Harness."""

    case_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    category: EvalCategory
    description: str
    workspace_snapshot: dict[str, str] = Field(default_factory=dict)  # relative_path -> content
    terminal_output: str = ""
    command: Optional[str] = None
    expected_files_changed: list[str] = Field(default_factory=list)
    expected_patch_keywords: list[str] = Field(default_factory=list)
    expected_no_safety_violations: bool = True
    tags: list[str] = Field(default_factory=list)


class EvalResult(BaseModel):
    """Result of running a single EvalCase through the pipeline."""

    case_id: str
    category: EvalCategory
    passed: bool = False
    safety_violations: list[SafetyViolationType] = Field(default_factory=list)
    proposal_summary: Optional[str] = None
    patch_files: list[str] = Field(default_factory=list)
    error_message: Optional[str] = None
    duration_seconds: float = Field(default=0.0, ge=0.0)
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def has_safety_violation(self) -> bool:
        return len(self.safety_violations) > 0


class EvalReport(BaseModel):
    """Aggregated report across all EvalResults in a run."""

    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    safety_violations: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    by_category: dict[str, dict[str, Any]] = Field(default_factory=dict)
    results: list[EvalResult] = Field(default_factory=list)
    ci_gate_passed: bool = False
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)


# ===========================================================================
# Q3: ECS / SBOM / Security Scan (설계서 §Q3)
# ===========================================================================

class ECSDeployStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    CIRCUIT_BREAKER_TRIGGERED = "circuit_breaker_triggered"


class SecurityScanSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class SecurityScanTool(str, Enum):
    TRIVY = "trivy"
    HADOLINT = "hadolint"
    GITLEAKS = "gitleaks"


class SecurityFinding(BaseModel):
    """보안 스캔 단일 발견 항목"""
    tool: SecurityScanTool
    severity: SecurityScanSeverity
    title: str
    description: str
    location: Optional[str] = None   # 파일명 또는 패키지명
    fix_suggestion: Optional[str] = None
    secret_redacted: bool = False    # gitleaks: 원문 미포함


class SecurityScanResult(BaseModel):
    """Trivy / Hadolint / gitleaks 통합 스캔 결과"""
    image: Optional[str] = None
    dockerfile_path: Optional[str] = None
    repo_path: Optional[str] = None
    findings: list[SecurityFinding] = Field(default_factory=list)
    critical_count: int = 0
    high_count: int = 0
    hadolint_error_count: int = 0
    secret_count: int = 0
    scan_passed: bool = False   # critical/hadolint_error/secret 모두 0이어야 True
    scanned_at: datetime = Field(default_factory=datetime.utcnow)

    def compute_pass(self) -> None:
        self.critical_count = sum(1 for f in self.findings
            if f.tool == SecurityScanTool.TRIVY and f.severity == SecurityScanSeverity.CRITICAL)
        self.high_count = sum(1 for f in self.findings
            if f.tool == SecurityScanTool.TRIVY and f.severity == SecurityScanSeverity.HIGH)
        self.hadolint_error_count = sum(1 for f in self.findings
            if f.tool == SecurityScanTool.HADOLINT and f.severity == SecurityScanSeverity.CRITICAL)
        self.secret_count = sum(1 for f in self.findings
            if f.tool == SecurityScanTool.GITLEAKS)
        self.scan_passed = (
            self.critical_count == 0
            and self.hadolint_error_count == 0
            and self.secret_count == 0
        )


class SBOMRecord(BaseModel):
    """SBOM 생성 결과 (Syft CycloneDX JSON)"""
    image: str
    sbom_path: str              # 로컬 파일 경로
    sbom_format: str = "cyclonedx-json"
    image_digest: Optional[str] = None
    package_count: int = 0
    vulnerability_summary: dict[str, int] = Field(default_factory=dict)  # severity → count
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class PreflightCheck(BaseModel):
    """Cloud Preflight 단일 항목 체크 결과"""
    name: str
    passed: bool
    detail: str
    severity: str = "error"   # "error" | "warning" | "info"
    fix_guide: Optional[str] = None


class PreflightReport(BaseModel):
    """Cloud Preflight 전체 리포트"""
    region: str
    cluster: str
    service: str
    checks: list[PreflightCheck] = Field(default_factory=list)
    passed: bool = False
    checked_at: datetime = Field(default_factory=datetime.utcnow)

    def compute_pass(self) -> None:
        error_checks = [c for c in self.checks if c.severity == "error"]
        self.passed = all(c.passed for c in error_checks)


class ECSDeployRequest(BaseModel):
    """ECS Rolling Update 요청 (Extension → Local Core)"""
    project_id: str
    cluster: str
    service: str
    region: str
    image: str                  # 배포할 이미지 (태그 포함)
    task_definition_family: str
    container_name: str
    health_check_path: str = "/health"
    environment: str = "production"
    cpu: str = "256"
    memory: str = "512"
    env_vars: dict[str, str] = Field(default_factory=dict)
    run_preflight: bool = True
    run_security_scan: bool = True
    generate_sbom: bool = True
    approval_request_id: Optional[str] = None  # OPA allow_with_approval 시


class ECSDeployRecord(BaseModel):
    """ECS 배포 기록 (DeploymentRecord 확장)"""
    deployment_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    cluster: str
    service: str
    region: str
    image: str
    image_digest: Optional[str] = None
    task_definition_arn: Optional[str] = None
    previous_task_definition_arn: Optional[str] = None  # rollback 대상
    status: ECSDeployStatus = ECSDeployStatus.PENDING
    preflight_passed: bool = False
    scan_result: Optional[SecurityScanResult] = None
    sbom_path: Optional[str] = None
    sbom_version: Optional[str] = None
    health_check_failures: int = 0
    circuit_breaker_triggered: bool = False
    rollback_proposal_id: Optional[str] = None
    deployed_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
