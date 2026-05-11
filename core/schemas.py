"""
ReCoder v6.4 공통 데이터 계약 (schemas.py)
설계서 §20 기준. 모든 에이전트가 이 파일을 import해서 사용한다.
변경 시 HANDOFF.md 업데이트 필수.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


SCHEMA_VERSION = "6.4"


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
    UNKNOWN        = "unknown"

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

class RiskLevel(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"

class ProjectStack(str, Enum):
    PYTHON_FASTAPI = "python-fastapi"
    PYTHON_FLASK   = "python-flask"
    NODE_EXPRESS   = "node-express"
    NODE_NEXT      = "node-next"
    CUSTOM         = "custom"

class DeployMethod(str, Enum):
    LOCAL_DOCKER   = "local_docker"
    SSH_DIRECT     = "ssh_direct"
    ECR_EC2        = "ecr_ec2"
    GITHUB_ACTIONS = "github_actions"

class DeployStatus(str, Enum):
    DEPLOYING   = "deploying"
    DEPLOYED    = "deployed"
    FAILED      = "failed"
    ROLLED_BACK = "rolled_back"

class ReadyStatus(str, Enum):
    OK      = "ok"
    PARTIAL = "partial"
    FAIL    = "fail"


# ── §20.1 ProjectProfile ──────────────────────────────────────────────

@dataclass
class ProjectProfile:
    project_id:         str
    workspace_path:     str
    stack:              ProjectStack
    package_manager:    str                    # pip | npm | yarn | pnpm
    default_run_command: str
    default_port:       int
    health_check_path:  str = "/health"
    dockerfile_path:    str = ""
    compose_path:       str = ""
    deployment_target:  str = ""               # local | ec2
    created_at:         str = ""
    updated_at:         str = ""

    def to_dict(self) -> dict:
        return {
            "project_id":          self.project_id,
            "workspace_path":      self.workspace_path,
            "stack":               self.stack.value,
            "package_manager":     self.package_manager,
            "default_run_command": self.default_run_command,
            "default_port":        self.default_port,
            "health_check_path":   self.health_check_path,
            "dockerfile_path":     self.dockerfile_path,
            "compose_path":        self.compose_path,
            "deployment_target":   self.deployment_target,
            "created_at":          self.created_at,
            "updated_at":          self.updated_at,
        }


# ── §20.2 AnalyzeRequest ─────────────────────────────────────────────
# Extension → Local Core로 전달되는 분석 요청

@dataclass
class AnalyzeRequest:
    workspace_path:        str
    terminal_output:       str
    project_id:            str = ""
    active_file_path:      str = ""
    selected_text:         str = ""
    command:               str = ""
    project_files_summary: str = ""            # 주요 파일 목록 요약
    # ── analyzer/code_agent 내부 처리용 파생 필드 (서버 또는 Context Gate가 채움) ──
    error_text:            str = ""            # terminal_output에서 추출된 에러 본문
    file_context:          str = ""            # 활성 파일 부근 코드 스니펫
    related_files:         list[str] = field(default_factory=list)  # 분석 대상 파일 경로 hint

    def to_dict(self) -> dict:
        return {
            "workspace_path":        self.workspace_path,
            "terminal_output":       self.terminal_output,
            "project_id":            self.project_id,
            "active_file_path":      self.active_file_path,
            "selected_text":         self.selected_text,
            "command":               self.command,
            "project_files_summary": self.project_files_summary,
            "error_text":            self.error_text,
            "file_context":          self.file_context,
            "related_files":         list(self.related_files),
        }


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


# ── §20.3 PatchProposal ──────────────────────────────────────────────

@dataclass
class FilePatch:
    file:         str
    base_sha256:  str
    unified_diff: str
    reason:       str = ""

    def to_dict(self) -> dict:
        return {
            "file":         self.file,
            "base_sha256":  self.base_sha256,
            "unified_diff": self.unified_diff,
            "reason":       self.reason,
        }

@dataclass
class PatchProposal:
    """§20.11 공통 필드: schema_version, risk_level, risk_reasons, approval_level"""
    proposal_id:    str
    summary:        str
    risk_level:     RiskLevel
    test_command:   str
    patches:        list[FilePatch]
    risk_reasons:   list[str] = field(default_factory=list)
    approval_level: int       = 1          # Level 1: 로컬 파일 수정
    schema_version: str       = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "proposal_id":    self.proposal_id,
            "summary":        self.summary,
            "risk_level":     self.risk_level.value,
            "risk_reasons":   self.risk_reasons,
            "approval_level": self.approval_level,
            "test_command":   self.test_command,
            "patches":        [p.to_dict() for p in self.patches],
        }


# ── §20.4 InfraFileProposal ──────────────────────────────────────────

@dataclass
class InfraFileProposal:
    """§20.11 공통 필드 포함"""
    proposal_id:      str
    file_type:        Literal["Dockerfile", "docker-compose", "github-actions"]
    target_path:      str
    content:          str
    base_template:    str
    risk_level:       RiskLevel
    risk_reasons:     list[str] = field(default_factory=list)
    required_secrets: list[str] = field(default_factory=list)
    approval_level:   int       = 1
    schema_version:   str       = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version":  self.schema_version,
            "proposal_id":     self.proposal_id,
            "file_type":       self.file_type,
            "target_path":     self.target_path,
            "content":         self.content,
            "base_template":   self.base_template,
            "risk_level":      self.risk_level.value,
            "risk_reasons":    self.risk_reasons,
            "required_secrets": self.required_secrets,
            "approval_level":  self.approval_level,
        }


# ── §20.5 DeploymentPlan ─────────────────────────────────────────────

@dataclass
class DeploymentPlan:
    """§20.11 공통 필드 포함"""
    plan_id:             str
    method:              DeployMethod
    action:              str
    image:               str
    container_name:      str
    command_template_id: str                   # CommandTemplate Registry 참조
    risk_level:          RiskLevel = RiskLevel.LOW
    risk_reasons:        list[str] = field(default_factory=list)
    approval_level:      int       = 2         # Level 2: 로컬 명령 실행
    schema_version:      str       = SCHEMA_VERSION
    ports:               list[dict] = field(default_factory=list)
    env:                 list[str]  = field(default_factory=list)
    health_check_path:   str       = "/health"
    rollback_image:      str       = ""

    def to_dict(self) -> dict:
        return {
            "schema_version":      self.schema_version,
            "plan_id":             self.plan_id,
            "method":              self.method.value,
            "action":              self.action,
            "image":               self.image,
            "container_name":      self.container_name,
            "command_template_id": self.command_template_id,
            "risk_level":          self.risk_level.value,
            "risk_reasons":        self.risk_reasons,
            "approval_level":      self.approval_level,
            "ports":               self.ports,
            "env":                 self.env,
            "health_check_path":   self.health_check_path,
            "rollback_image":      self.rollback_image,
        }


# ── §20.6 DeploymentRecord ───────────────────────────────────────────

@dataclass
class DeploymentRecord:
    deployment_id:     str
    project_id:        str
    method:            DeployMethod
    image:             str
    image_digest:      str
    git_commit:        str
    container_name:    str
    health_check_path: str
    deployed_at:       str
    status:            DeployStatus = DeployStatus.DEPLOYED
    rollback_target:   str = ""    # 롤백 시 이전 image_digest 또는 container_name

    def to_dict(self) -> dict:
        return {
            "deployment_id":    self.deployment_id,
            "project_id":       self.project_id,
            "method":           self.method.value,
            "image":            self.image,
            "image_digest":     self.image_digest,
            "git_commit":       self.git_commit,
            "container_name":   self.container_name,
            "health_check_path": self.health_check_path,
            "deployed_at":      self.deployed_at,
            "status":           self.status.value,
            "rollback_target":  self.rollback_target,
        }


# ── §20.7 AlertRecord (Stage 3, 2학기) ───────────────────────────────

@dataclass
class AlertRecord:
    alert_id:              str
    source:                str                 # "watchdog"
    project_id:            str
    environment:           str
    host:                  str
    container_name:        str
    alert_type:            str
    severity:              str
    detected_at:           str
    logs_excerpt:          str                 # masked
    health_check_result:   str
    metric_snapshot:       dict = field(default_factory=dict)
    recent_deployment_id:  str = ""
    fingerprint:           str = ""
    mask_version:          str = ""

    def to_dict(self) -> dict:
        return {
            "alert_id":             self.alert_id,
            "source":               self.source,
            "project_id":           self.project_id,
            "environment":          self.environment,
            "host":                 self.host,
            "container_name":       self.container_name,
            "alert_type":           self.alert_type,
            "severity":             self.severity,
            "detected_at":          self.detected_at,
            "logs_excerpt":         self.logs_excerpt,
            "health_check_result":  self.health_check_result,
            "metric_snapshot":      self.metric_snapshot,
            "recent_deployment_id": self.recent_deployment_id,
            "fingerprint":          self.fingerprint,
            "mask_version":         self.mask_version,
        }


# ── §20.8 ResponseProposal (Stage 3, 2학기) ──────────────────────────

@dataclass
class ResponseProposal:
    """§20.11 공통 필드 포함"""
    proposal_id:         str
    alert_id:            str
    action_type:         Literal["restart", "rollback", "env_check"]
    target_container:    str
    command_template_id: str
    parameters:          dict
    risk_level:          RiskLevel
    risk_reasons:        list[str] = field(default_factory=list)
    approval_level:      int       = 3         # Level 3: 원격 인프라
    schema_version:      str       = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version":      self.schema_version,
            "proposal_id":         self.proposal_id,
            "alert_id":            self.alert_id,
            "action_type":         self.action_type,
            "target_container":    self.target_container,
            "command_template_id": self.command_template_id,
            "parameters":          self.parameters,
            "risk_level":          self.risk_level.value,
            "risk_reasons":        self.risk_reasons,
            "approval_level":      self.approval_level,
        }


# ── §20.10 CommandTemplate / FileTemplate ────────────────────────────

@dataclass
class CommandTemplate:
    template_id:    str
    action_type:    str
    allowed_params: list[str]
    command_pattern: str
    risk_level:     RiskLevel
    approval_level: int
    version:        str = "1.0"

    def to_dict(self) -> dict:
        return {
            "template_id":    self.template_id,
            "action_type":    self.action_type,
            "allowed_params": self.allowed_params,
            "command_pattern": self.command_pattern,
            "risk_level":     self.risk_level.value,
            "approval_level": self.approval_level,
            "version":        self.version,
        }

@dataclass
class FileTemplate:
    template_id:          str
    file_type:            str
    base_content:         str
    customizable_sections: list[str]
    version:              str = "1.0"

    def to_dict(self) -> dict:
        return {
            "template_id":          self.template_id,
            "file_type":            self.file_type,
            "base_content":         self.base_content,
            "customizable_sections": self.customizable_sections,
            "version":              self.version,
        }


# ── §20.9 SessionRecord ──────────────────────────────────────────────

@dataclass
class LLMCallRecord:
    call_id:            str
    agent:              str
    operation:          str
    provider:           str
    model_identifier:   str
    region:             str = ""
    input_tokens:       int = 0
    output_tokens:      int = 0
    total_tokens:       int = 0
    token_source:       str = "local_estimate"
    estimated_cost_usd: float = 0.0
    latency_ms:         int = 0
    status:             Literal["success", "failed"] = "success"
    fallback_used:      bool = False
    retry_count:        int = 0
    error_type:         str | None = None

    def to_dict(self) -> dict:
        return {
            "call_id":            self.call_id,
            "agent":              self.agent,
            "operation":          self.operation,
            "provider":           self.provider,
            "model_identifier":   self.model_identifier,
            "region":             self.region,
            "input_tokens":       self.input_tokens,
            "output_tokens":      self.output_tokens,
            "total_tokens":       self.total_tokens,
            "token_source":       self.token_source,
            "estimated_cost_usd": self.estimated_cost_usd,
            "latency_ms":         self.latency_ms,
            "status":             self.status,
            "fallback_used":      self.fallback_used,
            "retry_count":        self.retry_count,
            "error_type":         self.error_type,
        }

@dataclass
class LLMUsageSummary:
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

@dataclass
class SessionEvent:
    time:                  str
    event_type:            str
    error_summary:         str
    error_fingerprint:     str
    related_file_names:    list[str]
    ai_suggestion_summary: str
    user_action:           Literal["approved", "rejected", "ignored"]
    result:                Literal["success", "failed", "pending"]
    validation:            Literal["test_passed", "syntax_ok", "unknown"]

@dataclass
class SessionRecord:
    session_id:           str
    start_time:           str
    project_id:           str
    end_time:             str | None = None
    events:               list[SessionEvent]   = field(default_factory=list)
    llm_calls:            list[LLMCallRecord]  = field(default_factory=list)
    llm_usage_summary:    LLMUsageSummary      = field(default_factory=LLMUsageSummary)
    raw_content_saved:    bool                 = False   # 항상 False 고정

    def add_llm_call(self, record: LLMCallRecord) -> None:
        self.llm_calls.append(record)
        self.llm_usage_summary.total_input_tokens  += record.input_tokens
        self.llm_usage_summary.total_output_tokens += record.output_tokens
        self.llm_usage_summary.estimated_total_cost_usd += record.estimated_cost_usd


# ── Orchestrator 상태 업데이트 (server → Extension 전달용) ─────────────

@dataclass
class OrchestratorUpdate:
    state:          OrchestratorState
    event:          AgentEvent | None         = None
    patch_proposal: PatchProposal | None      = None
    infra_proposal: InfraFileProposal | None  = None
    plan:           DeploymentPlan | None     = None
    message:        str                       = ""

    def to_dict(self) -> dict:
        return {
            "type":           "orchestrator_update",
            "state":          self.state.value,
            "event":          self.event.to_dict() if self.event else None,
            "patch_proposal": self.patch_proposal.to_dict() if self.patch_proposal else None,
            "infra_proposal": self.infra_proposal.to_dict() if self.infra_proposal else None,
            "plan":           self.plan.to_dict() if self.plan else None,
            "message":        self.message,
        }


# ── First Run Diagnostics ─────────────────────────────────────────────

@dataclass
class DiagnosticsResult:
    """~/.recoder/diagnostics.json 저장 구조 (§11)"""
    core_ready:         ReadyStatus = ReadyStatus.FAIL
    ai_ready:           ReadyStatus = ReadyStatus.FAIL
    docker_ready:       ReadyStatus = ReadyStatus.FAIL
    aws_deploy_ready:   ReadyStatus = ReadyStatus.FAIL   # 2학기
    ops_ready:          ReadyStatus = ReadyStatus.FAIL   # 2학기
    resolved_model_id:  str = ""
    resolved_region:    str = ""
    provider_type:      str = ""          # "bedrock" | "gemini" | ""
    is_cross_region_profile: bool = False
    validation_time:    str = ""
    docker_version:     str = ""
    issues:             list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "core_ready":               self.core_ready.value,
            "ai_ready":                 self.ai_ready.value,
            "docker_ready":             self.docker_ready.value,
            "aws_deploy_ready":         self.aws_deploy_ready.value,
            "ops_ready":                self.ops_ready.value,
            "resolved_model_id":        self.resolved_model_id,
            "resolved_region":          self.resolved_region,
            "provider_type":            self.provider_type,
            "is_cross_region_profile":  self.is_cross_region_profile,
            "validation_time":          self.validation_time,
            "docker_version":           self.docker_version,
            "issues":                   self.issues,
        }
