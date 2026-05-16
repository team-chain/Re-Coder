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
