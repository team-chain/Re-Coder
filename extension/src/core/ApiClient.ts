import {
    ApiResponse,
    CoreHealth,
    DiagnosticsResult,
    AnalyzeRequest,
    PatchProposal,
    InfraFileProposal,
    DeployMethod,
    DeploymentPlan,
    DeploymentRecord,
    AlertRecord,
    ResponseProposal,
    CostSummary,
    ProjectProfile,
    AwsStatus,
    AwsConfigureInput,
    AwsConnectInput,
    AwsEcrRepo,
} from '../types';
import { CoreManager } from './CoreManager';

export interface CodeSecretWarning {
    rule: string;
    severity: string;
    file: string;
    line: number;
    masked: string;
    fix: string;
}

export interface CodeAgentOp {
    action: 'create' | 'edit';
    file: string;
    language: string;
    content: string;
    rationale: string;
    secret_warnings?: CodeSecretWarning[];
}

export interface CodeAgentResult {
    summary: string;
    ops: CodeAgentOp[];
    model: string;
}

/** /api/code/plan — AI-DLC 1단계: 코드 대신 "설계 결정" 선택지. */
export interface CodeDecisionOption {
    key: string;
    label: string;
    summary: string;
    pros: string[];
    cons: string[];
    recommended: boolean;
}

export interface CodeDecision {
    id: string;
    question: string;
    options: CodeDecisionOption[];
    impact: string;
}

export interface CodePlanResult {
    decisions: CodeDecision[];
    model: string;
}

/** Workspace 오른쪽 대화 패널의 일반 AI 응답. 파일을 변경하지 않는 상담용 API다. */
export interface ChatResult {
    reply: string;
    model: string;
}

export interface DeployPreflightResult {
    app_kind: 'server' | 'static' | 'unknown';
    summary: string;
    evidence: string[];
    recommended_target: 'ecs' | 's3' | 'local';
}

export interface DeploymentDecisionResult {
    target: 'ecs' | 's3' | 'local';
    next_view: 'ecs' | 's3' | 'docker';
    adr: { file: string; content: string };
}

/** 사용자가 결정 카드(QuickPick)에서 고른 결과 — /api/code/generate 로 그대로 전달. */
export interface CodeDecisionChoice {
    id: string;
    question: string;
    chosen_key: string;
    options: CodeDecisionOption[];
}

export class ApiClient {
    constructor(private coreManager: CoreManager) {}

    private async request<T>(
        method: string,
        path: string,
        body?: unknown,
        _retried = false,
        timeoutMs = 30000
    ): Promise<ApiResponse<T>> {
        // 토큰이 비어있으면 runtime.json 에서 즉시 refresh.
        // ensureRunning() 완료 전 PollingService 가 호출하는 race condition 방지.
        let token = this.coreManager.getSessionToken();
        let port = this.coreManager.getPort();
        if (!token || !port || port <= 0 || !Number.isFinite(port)) {
            try { await this.coreManager.refreshToken(); } catch { /* ignore */ }
            token = this.coreManager.getSessionToken();
            port = this.coreManager.getPort();
        }
        // 여전히 port 가 없으면 요청 자체를 보내지 않고 부드럽게 실패 반환.
        // (이전엔 http://127.0.0.1:undefined/api/health 로 가서 fetch 가 throw)
        if (!port || port <= 0 || !Number.isFinite(port)) {
            return {
                success: false,
                error: 'Core not ready (port unavailable)',
                timestamp: new Date().toISOString(),
            };
        }
        const url = `http://127.0.0.1:${port}${path}`;

        const headers: Record<string, string> = {
            'Content-Type': 'application/json',
            'X-Session-Token': token,
        };

        const controller = new AbortController();
        const timerId = setTimeout(() => controller.abort(), timeoutMs);

        try {
            const res = await fetch(url, {
                method,
                headers,
                body: body !== undefined ? JSON.stringify(body) : undefined,
                signal: controller.signal,
            });

            clearTimeout(timerId);
            const timestamp = new Date().toISOString();

            if (!res.ok) {
                // 401/403 (Invalid session token) 발생 시 한 번은 무조건 토큰 재로드 후 재시도.
                // refreshToken 의 boolean 반환과 무관하게 retry (refresh 가 같은 토큰을
                // 다시 읽어와도 미들웨어가 다른 이유로 401/403 을 냈을 가능성 차단).
                if ((res.status === 401 || res.status === 403) && !_retried) {
                    try { await this.coreManager.refreshToken(); } catch { /* ignore */ }
                    return this.request<T>(method, path, body, true, timeoutMs);
                }
                let errorText = '';
                try { errorText = await res.text(); } catch { errorText = `HTTP ${res.status}`; }
                return { success: false, error: errorText || `HTTP ${res.status}`, timestamp };
            }

            const json = await res.json();
            if (typeof json === 'object' && json !== null && 'success' in json && 'timestamp' in json) {
                return json as ApiResponse<T>;
            }
            return { success: true, data: json as T, timestamp };
        } catch (err: unknown) {
            clearTimeout(timerId);
            const message = err instanceof Error ? err.message : 'Unknown network error';
            return { success: false, error: message, timestamp: new Date().toISOString() };
        }
    }

    async getHealth(): Promise<CoreHealth> {
        const resp = await this.request<CoreHealth & { uptime_seconds?: number }>('GET', '/api/health');
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? 'Health check 실패'); }
        // /api/health returns `uptime_seconds`; remap to the CoreHealth.uptime field.
        const raw = resp.data;
        return {
            ...raw,
            uptime: raw.uptime ?? (raw as unknown as { uptime_seconds?: number }).uptime_seconds ?? 0,
        };
    }

    async runDiagnostics(): Promise<DiagnosticsResult> {
        const resp = await this.request<DiagnosticsResult>('POST', '/api/diagnostics/run');
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? 'Diagnostics 실행 실패'); }
        return resp.data;
    }

    async getDiagnostics(): Promise<DiagnosticsResult | null> {
        const resp = await this.request<DiagnosticsResult>('GET', '/api/diagnostics');
        return resp.success && resp.data ? resp.data : null;
    }

    async analyze(request: AnalyzeRequest): Promise<PatchProposal | null> {
        const resp = await this.request<PatchProposal>('POST', '/api/analyze', request);
        return resp.success && resp.data ? resp.data : null;
    }

    /** Build 탭 코드 생성 에이전트 — 자연어 요청 → 파일 작업(ops). 적용은 호출측이 수행. */
    async generateCode(
        instruction: string,
        opts?: {
            workspacePath?: string;
            openFile?: { path: string; content: string };
            priorFiles?: Array<{ path: string; content: string }>;
            contextFiles?: Array<{ path: string; content: string }>;
            targetFolder?: string;
            // AI-DLC 2단계: 결정 카드에서 사용자가 승인한 선택 결과.
            // (Core 의 /api/code/generate 가 아직 이 필드를 소비하지 않아도 무해하게 무시됨 —
            //  decisions 반영은 별도 태스크.)
            decisions?: CodeDecisionChoice[];
        }
    ): Promise<CodeAgentResult> {
        const body = {
            instruction,
            workspace_path: opts?.workspacePath ?? '',
            open_file_path: opts?.openFile?.path ?? '',
            open_file_content: opts?.openFile?.content ?? '',
            prior_files: opts?.priorFiles ?? [],
            context_files: opts?.contextFiles ?? [],
            target_folder: opts?.targetFolder ?? '',
            decisions: opts?.decisions ?? [],
        };
        // 코드 생성은 30s 를 넘길 수 있어 90s 타임아웃.
        const resp = await this.request<CodeAgentResult>('POST', '/api/code/generate', body, false, 90000);
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '코드 생성 실패'); }
        return resp.data;
    }

    /**
     * AI-DLC 1단계 — 코드 대신 "설계 결정" 목록 요청 (/api/code/plan).
     * 결과는 SidebarProvider 가 QuickPick(팝업) 으로 렌더해 사용자에게 고르게 한다.
     */
    async planCode(
        instruction: string,
        opts?: {
            workspacePath?: string;
            openFile?: { path: string; content: string };
            contextFiles?: Array<{ path: string; content: string }>;
            targetFolder?: string;
        }
    ): Promise<CodePlanResult> {
        const body = {
            instruction,
            workspace_path: opts?.workspacePath ?? '',
            open_file_path: opts?.openFile?.path ?? '',
            open_file_content: opts?.openFile?.content ?? '',
            context_files: opts?.contextFiles ?? [],
            target_folder: opts?.targetFolder ?? '',
        };
        const resp = await this.request<CodePlanResult>('POST', '/api/code/plan', body, false, 60000);
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '설계 결정 생성 실패'); }
        return resp.data;
    }

    async getDeployPreflight(workspacePath: string): Promise<DeployPreflightResult> {
        const resp = await this.request<DeployPreflightResult>('POST', '/api/deploy/preflight', {
            workspace_path: workspacePath,
        });
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '배포 사전 감지 실패'); }
        return resp.data;
    }

    async recordDeploymentDecision(
        workspacePath: string,
        target: 'ecs' | 's3' | 'local',
        evidence: string[],
    ): Promise<DeploymentDecisionResult> {
        const resp = await this.request<DeploymentDecisionResult>('POST', '/api/deploy/decision', {
            workspace_path: workspacePath,
            target,
            evidence,
        });
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '배포 대상 결정 기록 실패'); }
        return resp.data;
    }

    /** 일반 대화형 AI — 코드 생성과 달리 이 호출만으로 파일을 수정하지 않는다. */
    async chat(
        message: string,
        history: Array<{ role: 'user' | 'assistant'; content: string }>,
        workspacePath = '',
    ): Promise<ChatResult> {
        const resp = await this.request<ChatResult>('POST', '/api/chat', {
            message,
            history,
            workspace_path: workspacePath,
        }, false, 90000);
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? 'AI 대화 실패'); }
        return resp.data;
    }

    /**
     * §38 Deploy Replay — Core 의 /api/replay/timeline 호출.
     * webview Replay.tsx 가 'loadReplay' 메시지로 deployId 를 보내면
     * SidebarProvider 가 이 메서드를 거쳐 결과를 'replayTimeline' 으로 회신.
     */
    async loadReplayTimeline(
        deployId: string,
        opts: { service?: string; cluster?: string; region?: string; windowHours?: number } = {},
    ): Promise<object | null> {
        const resp = await this.request<object>('POST', '/api/replay/timeline', {
            deploy_id: deployId,
            service: opts.service ?? '',
            cluster: opts.cluster ?? '',
            region: opts.region ?? 'ap-northeast-2',
            window_hours: opts.windowHours ?? 24,
        });
        return resp.success && resp.data ? resp.data : null;
    }

    async approvePatch(proposalId: string, approved: boolean): Promise<{ status: string }> {
        const resp = await this.request<{ status: string }>(
            'POST',
            `/api/analyze/approve?proposal_id=${encodeURIComponent(proposalId)}&approved=${approved}`,
        );
        return { status: resp.data?.status ?? (resp.success ? 'applied' : 'error') };
    }

    async listProposals(): Promise<PatchProposal[]> {
        const resp = await this.request<PatchProposal[]>('GET', '/api/analyze/proposals');
        return resp.success && resp.data ? resp.data : [];
    }

    async generateDockerfile(workspacePath: string, projectId?: string): Promise<InfraFileProposal> {
        const resp = await this.request<InfraFileProposal>(
            'POST', '/api/deploy/dockerfile',
            { workspace_path: workspacePath, project_id: projectId }
        );
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? 'Dockerfile 생성 실패'); }
        return resp.data;
    }

    async approveDockerfile(proposalId: string, approved: boolean): Promise<{ status: string }> {
        const resp = await this.request<{ status: string }>(
            'POST',
            `/api/deploy/dockerfile/approve?proposal_id=${encodeURIComponent(proposalId)}&approved=${approved}`,
        );
        return { status: resp.data?.status ?? (resp.success ? 'saved' : 'error') };
    }

    /**
     * GitHub Actions 워크플로우 YAML 생성.
     *
     * 설계 §4.1.2 (Ship Stage 확장): generate_github_actions() 결과를
     * InfraFileProposal 로 반환. 사용자 승인 후 .github/workflows/deploy.yml 에 저장.
     */
    async generateGithubActions(workspacePath: string, projectId?: string): Promise<InfraFileProposal> {
        const resp = await this.request<InfraFileProposal>(
            'POST', '/api/deploy/github-actions',
            { workspace_path: workspacePath, project_id: projectId }
        );
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? 'GitHub Actions 워크플로우 생성 실패'); }
        return resp.data;
    }

    /**
     * GitHub Actions 워크플로우 승인/저장.
     * approved=true 시 Core 가 .github/workflows/deploy.yml 에 저장.
     */
    async approveGithubActions(proposalId: string, approved: boolean): Promise<{ status: string }> {
        const resp = await this.request<{ status: string }>(
            'POST',
            `/api/deploy/github-actions/approve?proposal_id=${encodeURIComponent(proposalId)}&approved=${approved}`,
        );
        return { status: resp.data?.status ?? (resp.success ? 'saved' : 'error') };
    }

    // ── GitHub 인증 (VS Code OAuth → Core) ──────────────────────────────

    /** Core 에 GitHub 토큰 등록. VS Code 의 vscode.authentication.getSession() 으로 받은 토큰 전달. */
    async setGithubToken(token: string): Promise<{ status: string; user?: string; message?: string }> {
        const resp = await this.request<{ status: string; user?: string; message?: string }>(
            'POST', '/api/github/token', { token }
        );
        return resp.success && resp.data ? resp.data : { status: 'error', message: resp.error };
    }

    /** GitHub 인증 상태 조회 (캐시 5분). force=true 시 강제 갱신. */
    async getGithubStatus(force = false): Promise<{ status: string; user?: string; message?: string }> {
        const resp = await this.request<{ status: string; user?: string; message?: string }>(
            'GET', `/api/github/status${force ? '?force=true' : ''}`
        );
        return resp.success && resp.data ? resp.data : { status: 'error', message: resp.error };
    }

    /** GitHub 로그아웃 — Core 의 ~/.recoder/github.token 제거. */
    async githubLogout(): Promise<{ status: string; message?: string }> {
        const resp = await this.request<{ status: string; message?: string }>(
            'POST', '/api/github/logout'
        );
        return resp.success && resp.data ? resp.data : { status: 'error', message: resp.error };
    }

    /** 인증된 사용자의 GitHub 레포 목록. */
    async listGithubRepos(): Promise<{ status: string; repos: Array<{ name: string; private: boolean; html_url: string }> }> {
        const resp = await this.request<{ status: string; repos: unknown[] }>(
            'GET', '/api/github/repos'
        );
        if (!resp.success || !resp.data) {
            return { status: 'error', repos: [] };
        }
        return resp.data as { status: string; repos: Array<{ name: string; private: boolean; html_url: string }> };
    }

    async runScan(
        scanType: 'trivy' | 'hadolint' | 'gitleaks',
        workspacePath: string,
        targetPath?: string
    ): Promise<object> {
        const resp = await this.request<object>(
            'POST', '/api/deploy/scan',
            { workspace_path: workspacePath, scan_type: scanType, target_path: targetPath }
        );
        if (!resp.success || !resp.data) { throw new Error(`${scanType} 스캔 실패`); }
        return resp.data;
    }

    async createDeploymentPlan(
        workspacePath: string, method: DeployMethod, projectId?: string,
        image?: string, containerName?: string, hostPort?: number, containerPort?: number,
    ): Promise<DeploymentPlan> {
        // 플랜 생성은 Trivy/Hadolint 보안 게이트(도커 기반)를 돌려 30초를 넘길 수 있으므로 타임아웃을 길게.
        const resp = await this.request<DeploymentPlan>('POST', '/api/deploy/plan', {
            workspace_path: workspacePath, project_id: projectId, method,
            image, container_name: containerName, host_port: hostPort, container_port: containerPort,
        }, false, 600000);
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '배포 플랜 생성 실패'); }
        return resp.data;
    }

    async executeDeployment(planId: string, approved: boolean): Promise<{ status: string; deployment_id?: string; stdout?: string; stderr?: string }> {
        // docker build + run + 헬스체크는 30초를 넘으므로 타임아웃을 길게.
        const resp = await this.request<{ status: string; deployment_id?: string; stdout?: string; stderr?: string }>(
            'POST', '/api/deploy/execute', { plan_id: planId, approved }, false, 600000
        );
        return resp.success && resp.data ? resp.data : { status: 'error' };
    }

    async listDeploymentRecords(): Promise<DeploymentRecord[]> {
        const resp = await this.request<DeploymentRecord[]>('GET', '/api/deploy/records');
        return resp.success && resp.data ? resp.data : [];
    }

    async rollback(deploymentId: string): Promise<{ status: string }> {
        const resp = await this.request<{ status: string }>(
            'POST', '/api/deploy/rollback', { deployment_id: deploymentId }
        );
        return { status: resp.data?.status ?? (resp.success ? 'rolled_back' : 'error') };
    }

    // -----------------------------------------------------------------------
    // Ops / Incident Response
    // (paths match core/api/routes/ops.py exactly)
    // -----------------------------------------------------------------------

    async fetchIncidents(host: string, sshKeyPath: string, sshUser = 'ec2-user'): Promise<AlertRecord[]> {
        const resp = await this.request<AlertRecord[]>(
            'POST', '/api/ops/fetch-incidents',
            { host, ssh_key_path: sshKeyPath, ssh_user: sshUser }
        );
        return resp.success && resp.data ? resp.data : [];
    }

    async analyzeIncident(alertId: string, extraContext?: string): Promise<ResponseProposal> {
        const resp = await this.request<ResponseProposal>(
            'POST', '/api/ops/analyze',
            { alert_id: alertId, extra_context: extraContext }
        );
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '인시던트 분석 실패'); }
        return resp.data;
    }

    async approveResponse(
        proposalId: string,
        approved: boolean,
        sshHost?: string,
        sshUser?: string,
        sshKeyPath?: string,
    ): Promise<{ status: string }> {
        const resp = await this.request<{ status: string }>(
            'POST', '/api/ops/approve',
            { proposal_id: proposalId, approved, ssh_host: sshHost, ssh_user: sshUser, ssh_key_path: sshKeyPath }
        );
        return { status: resp.data?.status ?? (resp.success ? 'executed' : 'error') };
    }

    // -----------------------------------------------------------------------
    // Status (primary polling target for PollingService — §4.5)
    // -----------------------------------------------------------------------

    async getStatus(): Promise<{
        status: string;
        version: string;
        uptime_seconds: number;
        port: number;
        orchestrator_state: string;
        current_proposal_id: string | null;
        timestamp: string;
    }> {
        const resp = await this.request<{
            status: string;
            version: string;
            uptime_seconds: number;
            port: number;
            orchestrator_state: string;
            current_proposal_id: string | null;
            timestamp: string;
        }>('GET', '/api/status');
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '상태 조회 실패'); }
        return resp.data;
    }

    // -----------------------------------------------------------------------
    // Cost tracking (§19.5)
    // /api/cost  — design-doc canonical path
    // /api/session/cost — also supported by core for backwards compat
    // -----------------------------------------------------------------------

    async getCostSummary(): Promise<CostSummary> {
        const resp = await this.request<CostSummary>('GET', '/api/cost');
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '비용 정보 조회 실패'); }
        return resp.data;
    }

    // -----------------------------------------------------------------------
    // Project management (§20.1)
    // -----------------------------------------------------------------------

    async createProject(profile: ProjectProfile): Promise<ProjectProfile> {
        const resp = await this.request<ProjectProfile>('POST', '/api/projects', profile);
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '프로젝트 생성 실패'); }
        return resp.data;
    }

    async getProject(projectId: string): Promise<ProjectProfile | null> {
        const resp = await this.request<ProjectProfile>('GET', `/api/projects/${projectId}`);
        return resp.success && resp.data ? resp.data : null;
    }

    /**
     * GET /api/project — look up a ProjectProfile by workspace path.
     * Returns null if the workspace has not been scanned yet.
     */
    async getProjectByWorkspace(workspacePath: string): Promise<ProjectProfile | null> {
        const resp = await this.request<ProjectProfile>(
            'GET', `/api/project?workspace_path=${encodeURIComponent(workspacePath)}`
        );
        return resp.success && resp.data ? resp.data : null;
    }

    /**
     * POST /api/project/scan — auto-detect stack and upsert ProjectProfile.
     * Should be called when the sidebar is first activated (§20.1).
     */
    async scanProject(workspacePath: string, projectId?: string): Promise<ProjectProfile> {
        const resp = await this.request<ProjectProfile>(
            'POST', '/api/project/scan',
            { workspace_path: workspacePath, project_id: projectId }
        );
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '프로젝트 스캔 실패'); }
        return resp.data;
    }

    // -----------------------------------------------------------------------
    // Workbench (Discord ↔ Core ↔ VSCode 양방향 sync — D 역할)
    // /workbench/* — single source of truth: Core SQLite (3-Layer)
    // -----------------------------------------------------------------------

    async workbenchState(projectId?: string): Promise<{
        active_mode: 'home' | 'build' | 'ship' | 'operate' | 'recover';
        active_project_id: string | null;
        last_preflight: unknown;
        last_deployment: unknown;
        blockers_count: number;
        warnings_count: number;
        deployments_24h: number;
        rollback_available: boolean;
        recent_events: Array<{ kind: string; source: string; at: string; payload: object }>;
        as_of: string;
    }> {
        const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
        const resp = await this.request<never>('GET', `/workbench/state${qs}`);
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? 'Workbench state 조회 실패'); }
        return resp.data as never;
    }

    async workbenchChangeMode(
        mode: 'home' | 'build' | 'ship' | 'operate' | 'recover',
        source: 'vscode' | 'discord' | 'core' = 'vscode',
    ): Promise<{ active_mode: string; source: string }> {
        const resp = await this.request<{ active_mode: string; source: string }>(
            'POST', '/workbench/mode', { mode, source },
        );
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? 'Mode 전환 실패'); }
        return resp.data;
    }

    async workbenchPreflightRun(
        params: { project_id?: string; workspace_path?: string; source?: string } = {},
    ): Promise<{
        preflight_run_id: string;
        status: string;
        score: number;
        blockers: object[];
        warnings: object[];
    }> {
        const body = { source: 'vscode', ...params };
        const resp = await this.request<never>('POST', '/workbench/preflight/run', body);
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? 'Preflight 실행 실패'); }
        return resp.data as never;
    }

    async workbenchDeploymentStart(
        params: {
            project_id?: string;
            image_digest?: string;
            git_commit?: string;
            target_env?: 'local' | 'ec2' | 'ecs' | 'k8s';
            source?: string;
        } = {},
    ): Promise<{ deployment_id: string; status: string; preflight_run_id: string | null }> {
        const body = { source: 'vscode', target_env: 'local', ...params };
        const resp = await this.request<never>('POST', '/workbench/deployment/start', body);
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '배포 시작 실패'); }
        return resp.data as never;
    }

    async workbenchRollback(
        deploymentId: string,
        source: 'vscode' | 'discord' | 'core' = 'vscode',
    ): Promise<{ deployment_id: string; status: string }> {
        const resp = await this.request<{ deployment_id: string; status: string }>(
            'POST', `/workbench/deployment/${encodeURIComponent(deploymentId)}/rollback?source=${source}`,
        );
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? 'Rollback 실패'); }
        return resp.data;
    }

    async workbenchListDeployments(
        params: { project_id?: string; limit?: number } = {},
    ): Promise<unknown[]> {
        const qs = new URLSearchParams();
        if (params.project_id) { qs.set('project_id', params.project_id); }
        qs.set('limit', String(params.limit ?? 10));
        const resp = await this.request<unknown[]>('GET', `/workbench/deployments?${qs.toString()}`);
        return resp.success && resp.data ? resp.data : [];
    }

    async workbenchEvents(since: number = 0): Promise<{
        events: Array<{ kind: string; source: string; at: string; payload: object }>;
        next_offset: number;
    }> {
        const resp = await this.request<never>('GET', `/workbench/events?since=${since}`);
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '이벤트 조회 실패'); }
        return resp.data as never;
    }

    // -----------------------------------------------------------------------
    // AWS Credentials / Status (§S-2 — AWS Deploy Ready 활성화 흐름)
    // /api/aws/*  — see core/api/routes/aws.py
    // -----------------------------------------------------------------------

    /**
     * GET /api/aws/status — 현재 AWS 자격증명 상태.
     * 자격증명이 없어도 500 이 아닌 ready=false 의 200 응답을 보장한다.
     */
    async getAwsStatus(): Promise<AwsStatus> {
        const resp = await this.request<AwsStatus>('GET', '/api/aws/status');
        if (resp.success && resp.data) {
            return resp.data;
        }
        // network/401 등 실제 통신 실패 시 친화적 기본값
        return {
            ready: false,
            identity: null,
            region: '',
            profile: '',
            access_key_last4: '',
            storage: '',
            message: resp.error ?? 'AWS 상태 조회 실패',
        };
    }

    /** POST /api/aws/permissions/check — 현재 키를 다시 입력하지 않고 권한만 점검. */
    async checkAwsPermissions(): Promise<AwsStatus> {
        const resp = await this.request<AwsStatus>('POST', '/api/aws/permissions/check');
        if (!resp.success || !resp.data) {
            throw new Error(resp.error ?? 'AWS 권한 점검 실패');
        }
        return resp.data;
    }

    /**
     * POST /api/aws/configure — 자격증명 저장 + 즉시 STS 검증.
     * Core 가 검증을 통과해야만 디스크에 저장되고, diagnostics 캐시를 갱신한다.
     */
    async configureAws(creds: AwsConfigureInput): Promise<AwsStatus> {
        const body = {
            access_key_id: creds.accessKeyId,
            secret_access_key: creds.secretAccessKey,
            region: creds.region ?? '',
            profile: creds.profile ?? 'recoder',
            storage: creds.storage ?? 'recoder',
            session_token: creds.sessionToken ?? '',
        };
        const resp = await this.request<AwsStatus>('POST', '/api/aws/configure', body);
        if (!resp.success || !resp.data) {
            throw new Error(resp.error ?? 'AWS 자격증명 등록 실패');
        }
        return resp.data;
    }

    /**
     * POST /api/aws/connect — STS 검증만 수행한다.
     * 키의 영속 보관은 Extension의 SecretStorage가 담당한다.
     */
    async connectAws(creds: AwsConnectInput): Promise<AwsStatus> {
        const resp = await this.request<AwsStatus>('POST', '/api/aws/connect', {
            access_key_id: creds.accessKeyId,
            secret_access_key: creds.secretAccessKey,
            region: creds.region ?? '',
            session_token: creds.sessionToken ?? '',
        });
        if (!resp.success || !resp.data) {
            throw new Error(resp.error ?? 'AWS 자격증명 검증 실패');
        }
        return resp.data;
    }

    /**
     * POST /api/aws/clear — 저장된 자격증명 제거.
     */
    async clearAws(): Promise<void> {
        const resp = await this.request<{ status: string }>('POST', '/api/aws/clear');
        if (!resp.success) {
            throw new Error(resp.error ?? 'AWS 자격증명 제거 실패');
        }
    }

    /**
     * GET /api/aws/profiles — ~/.aws/credentials 의 사용 가능한 profile 목록.
     */
    async listAwsProfiles(): Promise<string[]> {
        const resp = await this.request<{ profiles: string[] }>('GET', '/api/aws/profiles');
        return resp.success && resp.data ? resp.data.profiles : [];
    }

    /**
     * GET /api/aws/ecr/repos — ECR 레포지토리 목록 (자격증명 sanity check).
     */
    async listEcrRepos(opts: { region?: string; profile?: string; maxResults?: number } = {}): Promise<AwsEcrRepo[]> {
        const qs = new URLSearchParams();
        if (opts.region) { qs.set('region', opts.region); }
        if (opts.profile) { qs.set('profile', opts.profile); }
        if (opts.maxResults) { qs.set('max_results', String(opts.maxResults)); }
        const path = qs.toString() ? `/api/aws/ecr/repos?${qs.toString()}` : '/api/aws/ecr/repos';
        const resp = await this.request<{ repositories: AwsEcrRepo[] }>('GET', path);
        return resp.success && resp.data ? resp.data.repositories : [];
    }

    // ===== Workbench 풀 구현 — Deploy / GitHub 액션 =====

    /** POST /api/deploy/ec2 — EC2 SSH 배포 시작 (백그라운드, status 폴링 필요). */
    async deployEc2(req: {
        workspace_path?: string;
        image_name?: string;
        repo_name?: string;
        tag?: string;
        container_name?: string;
        host_port?: number;
        container_port?: number;
        health_check_path?: string;
        ecr_registry?: string;
        ec2_host?: string;
        ec2_ssh_key?: string;
        aws_region?: string;
        ec2_user?: string;
    }): Promise<{ status: string; message: string }> {
        const resp = await this.request<{ status: string; message: string }>('POST', '/api/deploy/ec2', req);
        if (!resp.success || !resp.data) {
            throw new Error(resp.error ?? 'EC2 배포 요청 실패');
        }
        return resp.data;
    }

    /** GET /api/deploy/ec2/status — EC2 배포 진행상황 폴링. */
    async getEc2DeployStatus(): Promise<{
        running: boolean;
        stage: string;
        log_tail: string[];
        image_uri: string;
        error: string;
        started_at: string;
        finished_at: string;
    }> {
        const resp = await this.request<{
            running: boolean;
            stage: string;
            log_tail: string[];
            image_uri: string;
            error: string;
            started_at: string;
            finished_at: string;
        }>('GET', '/api/deploy/ec2/status');
        if (!resp.success || !resp.data) {
            throw new Error(resp.error ?? 'EC2 상태 조회 실패');
        }
        return resp.data;
    }

    /** GET /api/deploy/ec2/ready — EC2 배포 사전 점검. */
    async ec2DeployReady(): Promise<{ ready: boolean; issues: string[]; warnings?: string[] }> {
        const resp = await this.request<{ ready: boolean; issues: string[]; warnings?: string[] }>(
            'GET', '/api/deploy/ec2/ready'
        );
        return resp.success && resp.data ? resp.data : { ready: false, issues: ['Core 응답 없음'] };
    }

    /** POST /api/deploy/ecs — ECS Fargate 배포 시작. */
    async deployEcs(req: {
        workspace_path?: string;
        image_name?: string;
        repo_name?: string;
        tag?: string;
        ecr_registry?: string;
        ecs_cluster?: string;
        ecs_service?: string;
        aws_region?: string;
        container_name?: string;
        container_port?: number;
        cpu?: string;
        memory?: string;
        task_family?: string;
        environment?: string;
        branch?: string;
        skip_sbom?: boolean;
        skip_opa?: boolean;
    }): Promise<{ status: string; message: string }> {
        const resp = await this.request<{ status: string; message: string }>('POST', '/api/deploy/ecs', req);
        if (!resp.success || !resp.data) {
            throw new Error(resp.error ?? 'ECS 배포 요청 실패');
        }
        return resp.data;
    }

    /** GET /api/deploy/ecs/status — ECS 배포 진행상황 폴링. */
    async getEcsDeployStatus(): Promise<{
        running: boolean;
        stage: string;
        log_tail: string[];
        image_uri: string;
        task_def_arn: string;
        error: string;
        started_at: string;
        finished_at: string;
    }> {
        const resp = await this.request<{
            running: boolean;
            stage: string;
            log_tail: string[];
            image_uri: string;
            task_def_arn: string;
            error: string;
            started_at: string;
            finished_at: string;
        }>('GET', '/api/deploy/ecs/status');
        if (!resp.success || !resp.data) {
            throw new Error(resp.error ?? 'ECS 상태 조회 실패');
        }
        return resp.data;
    }

    /** GET /api/deploy/ecs/ready — ECS 배포 사전 점검. */
    async ecsDeployReady(): Promise<{ ready: boolean; issues: string[]; warnings?: string[] }> {
        const resp = await this.request<{ ready: boolean; issues: string[]; warnings?: string[] }>(
            'GET', '/api/deploy/ecs/ready'
        );
        return resp.success && resp.data ? resp.data : { ready: false, issues: ['Core 응답 없음'] };
    }

    /** POST /api/git/push — 원격 push (upstream 자동 설정). */
    async gitPush(req: { workspace_path: string; branch?: string; force?: boolean }): Promise<{
        status: string;
        message?: string;
        branch?: string;
    }> {
        const resp = await this.request<{ status: string; message?: string; branch?: string }>(
            'POST', '/api/git/push', req
        );
        if (!resp.success || !resp.data) {
            throw new Error(resp.error ?? 'git push 실패');
        }
        return resp.data;
    }

    /** GET /api/git/info — 현재 브랜치/remote 정보. */
    async gitInfo(workspacePath: string): Promise<{
        branch?: string;
        remote_url?: string;
        clean?: boolean;
        ahead?: number;
        behind?: number;
        last_commit?: string;
    }> {
        const qs = new URLSearchParams({ workspace_path: workspacePath });
        const resp = await this.request<object>('GET', `/api/git/info?${qs}`);
        return resp.success && resp.data ? (resp.data as object) : {};
    }

    /** POST /api/github/repo — 새 GitHub 레포 생성 + 초기 push. */
    async githubCreateRepo(req: {
        workspace_path: string;
        name: string;
        private: boolean;
        description?: string;
    }): Promise<{ status: string; html_url?: string; message?: string }> {
        const resp = await this.request<{ status: string; html_url?: string; message?: string }>(
            'POST', '/api/github/repo', req
        );
        if (!resp.success || !resp.data) {
            throw new Error(resp.error ?? '레포 생성 실패');
        }
        return resp.data;
    }

    /** POST /api/github/secret — GitHub Actions Secret 등록. */
    async githubSetSecret(req: { repo: string; name: string; value: string }): Promise<{
        status: string;
        message?: string;
    }> {
        const resp = await this.request<{ status: string; message?: string }>(
            'POST', '/api/github/secret', req
        );
        if (!resp.success || !resp.data) {
            throw new Error(resp.error ?? 'Secret 등록 실패');
        }
        return resp.data;
    }

    /** GET /api/github/runs?repo=owner/name — Actions 워크플로 실행 이력. */
    async githubListRuns(repo: string): Promise<{
        workflow_runs?: Array<{ id: number; name: string; status: string; conclusion: string; html_url: string }>;
    }> {
        const qs = new URLSearchParams({ repo });
        const resp = await this.request<{
            workflow_runs?: Array<{ id: number; name: string; status: string; conclusion: string; html_url: string }>;
        }>('GET', `/api/github/runs?${qs}`);
        return resp.success && resp.data ? resp.data : {};
    }
}
