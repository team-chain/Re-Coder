/**
 * Local Core REST API 클라이언트 (설계서 v6.4 §4.5)
 * Polling 기반 상태 동기화 (3~5초).
 *
 * 2026-05-08 갱신:
 * - /api/deploy/status, /api/security/scan, /api/ready, /api/patch/rollback 신규
 * - 응답 dataclass 의 통째 반환 형태에 맞춰 인터페이스 정리
 */
import * as http from 'http';

export interface HealthResponse {
    status: string;
    version: string;
    state: string;
    port: number;
}

/** OrchestratorUpdate.to_dict() 와 일치 */
export interface StatusResponse {
    type: string;
    state: string;
    event: object | null;
    patch_proposal: PatchProposalDto | null;
    infra_proposal: InfraProposalDto | null;
    plan: DeployPlanDto | null;
    message: string;
}

export interface FilePatchDto {
    file: string;
    base_sha256: string;
    unified_diff: string;
    reason: string;
}

export interface PatchProposalDto {
    schema_version: string;
    proposal_id: string;
    summary: string;
    risk_level: string;
    risk_reasons: string[];
    approval_level: number;
    test_command: string;
    patches: FilePatchDto[];
}

export interface InfraProposalDto {
    schema_version: string;
    proposal_id: string;
    file_type: 'Dockerfile' | 'docker-compose' | 'github-actions';
    target_path: string;
    content: string;
    base_template: string;
    risk_level: string;
    risk_reasons: string[];
    required_secrets: string[];
    approval_level: number;
}

export interface DeployPlanDto {
    schema_version: string;
    plan_id: string;
    method: string;
    action: string;
    image: string;
    container_name: string;
    command_template_id: string;
    risk_level: string;
    risk_reasons: string[];
    approval_level: number;
    ports: Array<{ host: number; container: number }>;
    env: string[];
    health_check_path: string;
    rollback_image: string;
}

export interface DeployStatusDto {
    stage: 'idle' | 'building' | 'running' | 'health' | 'done' | 'failed';
    log_tail: string[];
    health: boolean | null;
    finished: boolean;
    error: string;
    started_at: string;
    finished_at: string;
    state: string;
}

export interface ReadyDto {
    core_ready: 'ok' | 'partial' | 'fail';
    ai_ready: 'ok' | 'partial' | 'fail';
    docker_ready: 'ok' | 'partial' | 'fail';
    aws_deploy_ready?: string;
    ops_ready?: string;
    issues?: string[];
    [k: string]: any;
}

export interface SecurityScanDto {
    passed: boolean;
    results: {
        trivy?: { tool: string; passed: boolean; critical_count?: number; high_count?: number; findings?: any[]; summary?: string; error?: string };
        hadolint?: { tool: string; passed: boolean; findings?: any[]; summary?: string; error?: string };
    };
}

export interface CostDto {
    daily: number;
    monthly: number;
    calls: number;
}

export interface AnalyzeRequestPayload {
    workspace_path?: string;
    terminal_output?: string;
    project_id?: string;
    active_file_path?: string;
    selected_text?: string;
    command?: string;
    project_files_summary?: string;
    error_text?: string;
    file_context?: string;
    related_files?: string[];
}

export interface DeployLocalPayload {
    plan_id?: string;
    project_id?: string;
    workspace_path?: string;
    image?: string;
    container_name?: string;
    host_port?: number;
    container_port?: number;
    health_check_path?: string;
}

export class CoreClient {
    private readonly _base: string;
    private readonly _token: string;
    private _pollingTimer: ReturnType<typeof setInterval> | null = null;
    private _onStatusCallback: ((status: StatusResponse) => void) | null = null;

    constructor(portOrUrl: number | string, token: string) {
        if (typeof portOrUrl === 'string' && portOrUrl.startsWith('http')) {
            // 원격 서버 URL 직접 지정 (예: http://REDACTED-IP:8000)
            this._base = portOrUrl.replace(/\/$/, '');
        } else {
            this._base = `http://127.0.0.1:${portOrUrl}`;
        }
        this._token = token;
    }

    async healthCheck(): Promise<boolean> {
        try {
            const res = await this._get('/api/health') as HealthResponse;
            return res.status === 'ok';
        } catch { return false; }
    }

    async getStatus(): Promise<StatusResponse> {
        return this._get('/api/status') as Promise<StatusResponse>;
    }

    async getReady(): Promise<ReadyDto> {
        return this._get('/api/ready') as Promise<ReadyDto>;
    }

    // ── Stage 1 ────────────────────────────────────────────────────

    /** /api/analyze — PatchProposal 통째 반환 */
    async analyze(payload: AnalyzeRequestPayload): Promise<PatchProposalDto> {
        return this._post('/api/analyze', payload) as Promise<PatchProposalDto>;
    }

    async approvePatch(proposalId: string): Promise<{ status: string; applied_files: any[]; error: string; message: string }> {
        return this._post('/api/patch/approve', { proposal_id: proposalId }) as any;
    }

    async rejectPatch(proposalId: string): Promise<object> {
        return this._post('/api/patch/reject', { proposal_id: proposalId });
    }

    async rollbackPatch(proposalId: string): Promise<object> {
        return this._post('/api/patch/rollback', { proposal_id: proposalId });
    }

    // ── Stage 2 ────────────────────────────────────────────────────

    /** /api/infra/generate — InfraFileProposal 통째 반환 */
    async generateInfra(projectId: string, fileType: string, workspacePath?: string): Promise<InfraProposalDto> {
        return this._post('/api/infra/generate', {
            project_id: projectId,
            file_type: fileType,
            workspace_path: workspacePath ?? '',
        }) as Promise<InfraProposalDto>;
    }

    /** approve 응답에는 plan 이 같이 옴 (Dockerfile 의 경우) */
    async approveInfra(proposalId: string): Promise<{ status: string; saved_path: string; proposal_id: string; plan: DeployPlanDto | null; message: string }> {
        return this._post('/api/infra/approve', { proposal_id: proposalId }) as any;
    }

    /** /api/deploy/local — { plan, status } 반환, 실제 진행은 status polling */
    async deployLocal(payload: DeployLocalPayload): Promise<{ status: string; plan: DeployPlanDto; message: string }> {
        return this._post('/api/deploy/local', payload) as any;
    }

    async getDeployStatus(): Promise<DeployStatusDto> {
        return this._get('/api/deploy/status') as Promise<DeployStatusDto>;
    }

    async runSecurityScan(image: string, dockerfilePath?: string): Promise<SecurityScanDto> {
        return this._post('/api/security/scan', {
            image,
            dockerfile_path: dockerfilePath ?? '',
        }) as Promise<SecurityScanDto>;
    }

    // ── Project / Cost ─────────────────────────────────────────────

    async scanProject(workspacePath: string): Promise<object> {
        return this._post('/api/project/scan', { workspace_path: workspacePath });
    }

    async getProject(): Promise<object> {
        return this._get('/api/project');
    }

    async getCost(): Promise<CostDto> {
        return this._get('/api/cost') as Promise<CostDto>;
    }

    // ── S-8(TS): Git 커밋 ─────────────────────────────────────────

    async gitCommit(
        workspacePath: string,
        message: string,
        sessionId: string = ''
    ): Promise<{ status: string; commit_hash: string; message: string }> {
        return this._post('/api/git/commit', {
            workspace_path: workspacePath,
            message,
            session_id: sessionId,
        }) as any;
    }

    // ── Git GUI 패널 ──────────────────────────────────────────────

    /** 저장소 상태 조회 (브랜치, remote, uncommitted, ahead/behind, gh 사용자) */
    async gitInfo(workspacePath: string, forceRefresh = false): Promise<{
        status: string; is_git_repo: boolean; branch: string; remote_url: string;
        has_remote: boolean; uncommitted: number; ahead: number; behind: number; gh_user: string;
    }> {
        const qs = `/api/git/info?workspace_path=${encodeURIComponent(workspacePath)}${forceRefresh ? '&force_refresh=true' : ''}`;
        return this._get(qs) as any;
    }

    /** 원격 저장소(origin) URL 변경 */
    async gitSetRemote(workspacePath: string, repoFullName: string): Promise<{
        status: string; remote_url: string; message: string;
    }> {
        return this._post('/api/git/set-remote', {
            workspace_path: workspacePath,
            repo_full_name: repoFullName,
        }) as any;
    }

    /** 로컬/원격 브랜치 목록 */
    async gitBranches(workspacePath: string): Promise<{
        status: string; branches: string[]; current: string; remote_branches: string[];
    }> {
        return this._get(`/api/git/branches?workspace_path=${encodeURIComponent(workspacePath)}`) as any;
    }

    /** 브랜치 전환 */
    async gitCheckout(workspacePath: string, branch: string): Promise<{
        status: string; branch: string; message: string;
    }> {
        return this._post('/api/git/checkout', { workspace_path: workspacePath, branch }) as any;
    }

    /** 새 브랜치 생성 */
    async gitBranchCreate(workspacePath: string, branchName: string, checkout: boolean = true): Promise<{
        status: string; branch: string; message: string;
    }> {
        return this._post('/api/git/branch/create', {
            workspace_path: workspacePath, branch_name: branchName, checkout,
        }) as any;
    }

    /** 원격 push */
    async gitPush(workspacePath: string, branch: string = '', force: boolean = false): Promise<{
        status: string; message: string; remote_url: string;
    }> {
        return this._post('/api/git/push', {
            workspace_path: workspacePath, branch, force,
        }) as any;
    }

    // ── S-9(TS): 롤백 ────────────────────────────────────────────

    async deployRollback(
        planId: string
    ): Promise<{ status: string; message: string; logs: string[] }> {
        return this._post('/api/deploy/rollback', { plan_id: planId }) as any;
    }

    // ── EC2 배포 ─────────────────────────────────────────────────────

    async deployEC2Ready(): Promise<{ ready: boolean; issues: string[] }> {
        return this._get('/api/deploy/ec2/ready') as any;
    }

    async deployEC2(payload: {
        workspace_path?: string;
        image_name?: string;
        repo_name?: string;
        tag?: string;
        container_name?: string;
        host_port?: number;
        container_port?: number;
        health_check_path?: string;
        env_vars?: string[];
        ecr_registry?: string;
        ec2_host?: string;
        ec2_ssh_key?: string;
        aws_region?: string;
        ec2_user?: string;
    }): Promise<{ status: string; message: string }> {
        return this._post('/api/deploy/ec2', payload) as any;
    }

    async getEC2DeployStatus(): Promise<{
        running: boolean;
        stage: string;
        log_tail: string[];
        image_uri: string;
        error: string;
        started_at: string;
        finished_at: string;
    }> {
        return this._get('/api/deploy/ec2/status') as any;
    }

    // ── ECS Fargate 배포 (Q3-A) ──────────────────────────────────────

    async deployECSReady(): Promise<{ ready: boolean; issues: string[] }> {
        return this._get('/api/deploy/ecs/ready') as any;
    }

    async deployECS(payload: {
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
        env_vars?: Array<{ name: string; value: string }>;
        task_family?: string;
        environment?: string;
        branch?: string;
        skip_sbom?: boolean;
        skip_opa?: boolean;
    }): Promise<{ status: string; message: string }> {
        return this._post('/api/deploy/ecs', payload) as any;
    }

    async getECSDeployStatus(): Promise<{
        running: boolean;
        stage: string;
        log_tail: string[];
        image_uri: string;
        task_def_arn: string;
        error: string;
        started_at: string;
        finished_at: string;
        rollback_proposal: object | null;
    }> {
        return this._get('/api/deploy/ecs/status') as any;
    }

    async opaReady(): Promise<{ available: boolean; url: string; note: string }> {
        return this._get('/api/opa/ready') as any;
    }

    async generateSBOM(image_uri: string, tag?: string): Promise<{
        success: boolean;
        sbom_path: string;
        package_count: number;
        sbom_hash: string;
        error: string;
    }> {
        return this._post('/api/sbom/generate', { image_uri, tag: tag ?? 'latest' }) as any;
    }

    // ── GitHub one-click ──────────────────────────────────────────

    async ghStatus(force: boolean = false): Promise<{ installed: boolean; version: string; authed: boolean; user: string; install_hint: string }> {
        return this._get(`/api/github/status${force ? '?force=true' : ''}`) as any;
    }

    async ghRepos(): Promise<{ status: string; repos: Array<{ name: string; full_name: string; private: boolean; url: string; description: string }> }> {
        return this._get('/api/github/repos') as any;
    }

    /**
     * Extension 이 vscode.authentication.getSession() 으로 획득한 GitHub 토큰을 Core 에 저장.
     * Core 는 /user API 로 유효성 검증 후 { status, user } 반환.
     */
    async ghSetToken(token: string): Promise<{ status: string; user: string; message?: string }> {
        return this._post('/api/github/token', { token }) as any;
    }

    /** @deprecated VS Code OAuth 방식으로 대체됨 — ghSetToken() 사용 */
    async ghLoginBegin(): Promise<{ status: string; code: string; verify_url: string; message: string }> {
        return this._post('/api/github/login', {}) as any;
    }

    /** @deprecated VS Code OAuth 방식으로 대체됨 */
    async ghLoginPoll(): Promise<{ stage: string; code: string; verify_url: string; user: string; error: string }> {
        return this._get('/api/github/login/poll') as any;
    }

    /** @deprecated VS Code OAuth 방식으로 대체됨 */
    async ghLoginCancel(): Promise<{ status: string }> {
        return this._post('/api/github/login/cancel', {}) as any;
    }

    async shipGitHub(payload: {
        workspace_path: string;
        repo_name: string;
        private: boolean;
        description?: string;
        secrets?: Record<string, string>;
        include_dockerfile?: boolean;
        include_compose?: boolean;
        include_actions?: boolean;
        include_dockerignore?: boolean;
    }): Promise<{ status: string; message: string }> {
        return this._post('/api/ship/github', payload) as any;
    }

    async shipGitHubStatus(): Promise<{
        running: boolean;
        steps: { id: string; label: string; status: string; message: string }[];
        current: string;
        error: string;
        repo_url: string;
        started_at: string;
        finished_at: string;
    }> {
        return this._get('/api/ship/github/status') as any;
    }

    async ghBranches(workspacePath: string): Promise<{ branches: string[]; current: string; error: string }> {
        return this._get(`/api/github/branches?workspace_path=${encodeURIComponent(workspacePath)}`) as any;
    }

    async ghLogout(): Promise<{ status: string; message?: string }> {
        return this._post('/api/github/logout', {}) as any;
    }

    // ── Polling 헬퍼 (CoreManager / Sidebar 가 사용) ──────────────

    startPolling(intervalMs: number = 4000, callback: (s: StatusResponse) => void): void {
        this._onStatusCallback = callback;
        this._pollingTimer = setInterval(async () => {
            try {
                const status = await this.getStatus();
                this._onStatusCallback?.(status);
            } catch { /* Core 재연결 시도는 CoreManager 가 담당 */ }
        }, intervalMs);
    }

    stopPolling(): void {
        if (this._pollingTimer) clearInterval(this._pollingTimer);
        this._pollingTimer = null;
    }

    // ── HTTP ──────────────────────────────────────────────────────

    private async _get(path: string): Promise<object> {
        return new Promise((resolve, reject) => {
            const req = http.get(`${this._base}${path}`, {
                headers: { 'X-Session-Token': this._token }
            }, (res) => {
                let data = '';
                res.on('data', c => data += c);
                res.on('end', () => {
                    if (res.statusCode && res.statusCode >= 400) {
                        try {
                            const err = JSON.parse(data || '{}');
                            reject(new Error(err.detail || `HTTP ${res.statusCode}`));
                        } catch {
                            reject(new Error(`HTTP ${res.statusCode}: ${data.slice(0, 200)}`));
                        }
                        return;
                    }
                    try { resolve(JSON.parse(data || 'null')); }
                    catch (e) { reject(e); }
                });
            });
            req.on('error', reject);
            req.setTimeout(8000, () => { req.destroy(); reject(new Error('timeout')); });
        });
    }

    private async _post(path: string, body: object): Promise<object> {
        const payload = JSON.stringify(body ?? {});
        return new Promise((resolve, reject) => {
            const req = http.request(`${this._base}${path}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Content-Length': Buffer.byteLength(payload),
                    'X-Session-Token': this._token,
                    'Origin': this._base,
                }
            }, (res) => {
                let data = '';
                res.on('data', c => data += c);
                res.on('end', () => {
                    if (res.statusCode && res.statusCode >= 400) {
                        try {
                            const err = JSON.parse(data || '{}');
                            reject(new Error(err.detail || `HTTP ${res.statusCode}`));
                        } catch {
                            reject(new Error(`HTTP ${res.statusCode}: ${data.slice(0, 200)}`));
                        }
                        return;
                    }
                    try { resolve(JSON.parse(data || 'null')); }
                    catch (e) { reject(e); }
                });
            });
            req.on('error', reject);
            // analyze/deploy 는 LLM·docker 호출이라 타임아웃 길게
            req.setTimeout(120_000, () => { req.destroy(); reject(new Error('timeout')); });
            req.write(payload);
            req.end();
        });
    }
}
