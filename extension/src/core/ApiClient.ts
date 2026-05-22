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
} from '../types';
import { CoreManager } from './CoreManager';

export class ApiClient {
    constructor(private coreManager: CoreManager) {}

    private async request<T>(
        method: string,
        path: string,
        body?: unknown,
        _retried = false
    ): Promise<ApiResponse<T>> {
        // 토큰이 비어있으면 runtime.json 에서 즉시 refresh.
        // ensureRunning() 완료 전 PollingService 가 호출하는 race condition 방지.
        let token = this.coreManager.getSessionToken();
        if (!token) {
            try { await this.coreManager.refreshToken(); } catch { /* ignore */ }
            token = this.coreManager.getSessionToken();
        }
        const port = this.coreManager.getPort();
        const url = `http://127.0.0.1:${port}${path}`;

        const headers: Record<string, string> = {
            'Content-Type': 'application/json',
            'X-Session-Token': token,
        };

        const controller = new AbortController();
        const timerId = setTimeout(() => controller.abort(), 30000);

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
                // 401 발생 시 한 번은 무조건 토큰 재로드 후 재시도.
                // refreshToken 의 boolean 반환과 무관하게 retry (refresh 가 같은 토큰을
                // 다시 읽어와도 미들웨어가 다른 이유로 401을 냈을 가능성 차단).
                if (res.status === 401 && !_retried) {
                    try { await this.coreManager.refreshToken(); } catch { /* ignore */ }
                    return this.request<T>(method, path, body, true);
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
        const resp = await this.request<DeploymentPlan>('POST', '/api/deploy/plan', {
            workspace_path: workspacePath, project_id: projectId, method,
            image, container_name: containerName, host_port: hostPort, container_port: containerPort,
        });
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '배포 플랜 생성 실패'); }
        return resp.data;
    }

    async executeDeployment(planId: string, approved: boolean): Promise<{ status: string; deployment_id?: string }> {
        const resp = await this.request<{ status: string; deployment_id?: string }>(
            'POST', '/api/deploy/execute', { plan_id: planId, approved }
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
}
