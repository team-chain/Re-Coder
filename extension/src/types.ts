/**
 * ReCoder Extension — TypeScript Type Definitions
 * 1:1 correspondence with core/schemas.py (Section 20 Data Contracts)
 */

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

export enum RiskLevel {
  LOW = "low",
  MEDIUM = "medium",
  HIGH = "high",
  CRITICAL = "critical",
}

export enum ApprovalLevel {
  AUTO = 1,           // No confirmation required
  CONFIRM = 2,        // Single confirmation
  DOUBLE_CONFIRM = 3, // Two-step confirmation
  BLOCKED = 4,        // Cannot be executed
}

export enum StackType {
  PYTHON_FASTAPI = "python-fastapi",
  PYTHON_FLASK = "python-flask",
  PYTHON_DJANGO = "python-django",
  NODE_EXPRESS = "node-express",
  NODE_NEXT = "node-next",
  NODE_NEST = "node-nest",
  GO = "go",
  JAVA_SPRING = "java-spring",
  RUBY_RAILS = "ruby-rails",
  STATIC = "static",
  UNKNOWN = "unknown",
}

export enum DeployMethod {
  LOCAL_DOCKER = "local_docker",
  SSH_DOCKER = "ssh_docker",
  AWS_ECS = "aws_ecs",
  AWS_LAMBDA = "aws_lambda",
  K8S = "k8s",
}

export enum AlertType {
  CRASH = "crash",
  HIGH_CPU = "high_cpu",
  HIGH_MEMORY = "high_memory",
  HEALTH_CHECK_FAIL = "health_check_fail",
  OOM = "oom",
  DEPLOY_FAILURE = "deploy_failure",
  DISK_PRESSURE = "disk_pressure",
  LATENCY_SPIKE = "latency_spike",
  ERROR_RATE_SPIKE = "error_rate_spike",
  CUSTOM = "custom",
}

export enum ActionType {
  DOCKER_BUILD = "docker_build",
  DOCKER_RUN = "docker_run",
  DOCKER_STOP = "docker_stop",
  DOCKER_RESTART = "docker_restart",
  DOCKER_LOGS = "docker_logs",
  SSH_DOCKER_RESTART = "ssh_docker_restart",
  SSH_DOCKER_ROLLBACK = "ssh_docker_rollback",
  SSH_ENV_UPDATE = "ssh_env_update",
  ECR_LOGIN = "ecr_login",
  ECR_PUSH = "ecr_push",
  ECR_PULL = "ecr_pull",
  SCALE_UP = "scale_up",
  SCALE_DOWN = "scale_down",
  NOTIFY = "notify",
  NO_ACTION = "no_action",
}

export enum FileType {
  DOCKERFILE = "dockerfile",
  DOCKER_COMPOSE = "docker_compose",
  GITHUB_ACTIONS = "github_actions",
  NGINX_CONF = "nginx_conf",
  ENV_FILE = "env_file",
  K8S_MANIFEST = "k8s_manifest",
  TERRAFORM = "terraform",
}

export enum ProviderType {
  ANTHROPIC = "anthropic",
  BEDROCK = "bedrock",
  OPENAI = "openai",
  LOCAL = "local",
}

export enum ReadyState {
  READY = "ready",
  NOT_READY = "not_ready",
  PARTIAL = "partial",
  ERROR = "error",
}

export enum Mode {
  BUILD = "build",
  SHIP = "ship",
  OPERATE = "operate",
}

export enum DeployStatus {
  PENDING = "pending",
  IN_PROGRESS = "in_progress",
  SUCCESS = "success",
  FAILED = "failed",
  ROLLED_BACK = "rolled_back",
  CANCELLED = "cancelled",
}

// ---------------------------------------------------------------------------
// Core Domain Interfaces
// ---------------------------------------------------------------------------

export interface ProjectProfile {
  project_id: string;
  workspace_path: string;
  stack: StackType;
  package_manager?: string;
  default_run_command?: string;
  default_port?: number;
  health_check_path: string;
  dockerfile_path?: string;
  compose_path?: string;
  deployment_target: DeployMethod;
  created_at: string; // ISO 8601
  updated_at: string; // ISO 8601
}

export interface AnalyzeRequest {
  workspace_path: string;
  active_file_path?: string;
  selected_text?: string;
  terminal_output?: string;
  command?: string;
  project_files_summary?: string;
  project_id?: string;
}

// ---------------------------------------------------------------------------
// Patch & Proposal Interfaces
// ---------------------------------------------------------------------------

export interface FilePatch {
  file: string;
  base_sha256?: string;
  unified_diff: string;
  reason: string;
}

export interface PatchProposal {
  schema_version: string;
  proposal_id: string;
  summary: string;
  risk_level: RiskLevel;
  risk_reasons: string[];
  approval_level: ApprovalLevel;
  patches: FilePatch[];
  test_command?: string;
}

export interface InfraFileProposal {
  schema_version: string;
  proposal_id: string;
  file_type: FileType;
  target_path: string;
  content: string;
  base_template?: string;
  required_secrets: string[];
  risk_level: RiskLevel;
  risk_reasons: string[];
  approval_level: ApprovalLevel;
}

// ---------------------------------------------------------------------------
// Deployment Interfaces
// ---------------------------------------------------------------------------

export interface DeploymentPlan {
  schema_version: string;
  plan_id: string;
  method: DeployMethod;
  action: ActionType;
  image?: string;
  container_name?: string;
  ports: Record<string, string>; // host_port -> container_port
  env: Record<string, string>;
  health_check_path: string;
  rollback_image?: string;
  command_template_id?: string;
  risk_level: RiskLevel;
  risk_reasons: string[];
  approval_level: ApprovalLevel;
}

export interface DeploymentRecord {
  deployment_id: string;
  project_id: string;
  method: DeployMethod;
  image: string;
  image_digest?: string;
  git_commit?: string;
  container_name: string;
  health_check_path: string;
  deployed_at: string; // ISO 8601
  rollback_target?: string;
  status: DeployStatus;
}

// ---------------------------------------------------------------------------
// Alerting & Ops Interfaces
// ---------------------------------------------------------------------------

export interface AlertRecord {
  alert_id: string;
  source: string;
  project_id?: string;
  environment: string;
  host?: string;
  container_name?: string;
  alert_type: AlertType;
  severity: RiskLevel;
  detected_at: string; // ISO 8601
  logs_excerpt?: string;
  health_check_result?: HealthCheckResult;
  metric_snapshot: Record<string, unknown>;
  recent_deployment_id?: string;
  fingerprint?: string;
  mask_version?: string;
}

export interface ResponseProposal {
  schema_version: string;
  proposal_id: string;
  alert_id: string;
  action_type: ActionType;
  target_container?: string;
  command_template_id?: string;
  parameters: Record<string, unknown>;
  risk_level: RiskLevel;
  risk_reasons: string[];
  approval_level: ApprovalLevel;
}

// ---------------------------------------------------------------------------
// LLM & Session Interfaces
// ---------------------------------------------------------------------------

export interface LLMCallRecord {
  call_id: string;
  agent: string;
  operation: string;
  provider: ProviderType;
  model: string;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: number;
  latency_ms: number;
  token_source: string;
  fallback_used: boolean;
  retry_count: number;
  timestamp: string; // ISO 8601
}

export interface SessionRecord {
  schema_version: string;
  session_id: string;
  start_time: string; // ISO 8601
  end_time?: string;  // ISO 8601
  project_id?: string;
  events: Record<string, unknown>[];
  llm_calls: LLMCallRecord[];
  llm_usage_summary: Record<string, unknown>;
  raw_content_saved: boolean;
}

// ---------------------------------------------------------------------------
// Diagnostic & Runtime Interfaces
// ---------------------------------------------------------------------------

export interface DiagnosticsResult {
  core_ready: ReadyState;
  ai_ready: ReadyState;
  docker_ready: ReadyState;
  aws_deploy_ready: ReadyState;
  ops_ready: ReadyState;
  resolved_model_id?: string;
  resolved_region?: string;
  is_cross_region_profile: boolean;
  provider_type?: ProviderType;
  validation_time: string; // ISO 8601
  details: Record<string, unknown>;
}

export interface RuntimeConfig {
  port: number;
  session_token: string;
  pid: number;
  started_at: string; // ISO 8601
}

// ---------------------------------------------------------------------------
// Utility / Supporting Interfaces
// ---------------------------------------------------------------------------

export interface HealthCheckResult {
  status: "healthy" | "unhealthy" | "timeout" | "error";
  latency_ms?: number;
  checked_at: string; // ISO 8601
}

export interface CostSummary {
  daily_usd: number;
  monthly_usd: number;
  call_count: number;
  last_updated: string; // ISO 8601
}

// ---------------------------------------------------------------------------
// AWS Status (§S-2 — /api/aws/* 라우트 1:1 매핑)
// ---------------------------------------------------------------------------

export interface AwsIdentity {
  account: string;
  arn: string;
  user_id: string;
}

export interface AwsStatus {
  ready: boolean;
  identity?: AwsIdentity | null;
  region: string;
  profile: string;
  access_key_last4: string;
  /** "recoder" | "aws_credentials_file" | "env" | "" */
  storage: string;
  message: string;
}

export interface AwsConfigureInput {
  accessKeyId: string;
  secretAccessKey: string;
  region?: string;
  profile?: string;
  /** "recoder" (default) | "aws_credentials_file" */
  storage?: 'recoder' | 'aws_credentials_file';
  sessionToken?: string;
}

/** 저장 없이 STS 검증에만 쓰는 AWS 입력값. */
export interface AwsConnectInput {
  accessKeyId: string;
  secretAccessKey: string;
  region?: string;
  sessionToken?: string;
}

export interface AwsEcrRepo {
  name: string;
  uri: string;
  arn: string;
  created_at: string;
  image_tag_mutability: string;
}

// ---------------------------------------------------------------------------
// API Response Wrapper
// ---------------------------------------------------------------------------

export interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: string;
  request_id?: string;
  timestamp: string; // ISO 8601
}

// ---------------------------------------------------------------------------
// Extension UI State
// ---------------------------------------------------------------------------

export interface SidebarState {
  currentMode: Mode;
  proposals: (PatchProposal | InfraFileProposal | ResponseProposal)[];
  diagnostics?: DiagnosticsResult;
  costSummary?: CostSummary;
  isLoading: boolean;
  error?: string;
}

export interface TerminalOutput {
  command: string;
  output: string;
  exitCode: number;
  timestamp: string; // ISO 8601
}

export interface CoreHealth {
  status: "ok" | "degraded" | "down";
  version: string;
  uptime: number; // seconds
  port: number;
  /** Orchestrator FSM state — populated by /api/status (§4.5) */
  orchestrator_state?: OrchestratorState;
  /** proposal_id currently being processed, if any */
  current_proposal_id?: string | null;
  /** ISO 8601 timestamp from the server */
  timestamp?: string;
}

/**
 * Orchestrator FSM states — mirrors OrchestratorState enum in orchestrator.py
 */
export type OrchestratorState =
  | "idle"
  | "collecting_context"
  | "masking"
  | "scoring"
  | "analyzing"
  | "proposing"
  | "awaiting_approval"
  | "applying"
  | "rolling_back"
  | "complete"
  | "error";
