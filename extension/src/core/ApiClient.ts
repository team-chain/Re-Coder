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
        body?: unknown
    ): Promise<ApiResponse<T>> {
        const port = this.coreManager.getPort();
        const token = this.coreManager.getSessionToken();
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
        const resp = await this.request<CoreHealth>('GET', '/api/health');
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? 'Health check 실패'); }
        return resp.data;
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

    async fetchIncidents(host: string, sshKeyPath: string, sshUser = 'ec2-user'): Promise<AlertRecord[]> {
        const resp = await this.request<AlertRecord[]>(
            'POST', '/api/ops/incidents',
            { host, ssh_key_path: sshKeyPath, ssh_user: sshUser }
        );
        return resp.success && resp.data ? resp.data : [];
    }

    async analyzeIncident(alertId: string): Promise<ResponseProposal> {
        const resp = await this.request<ResponseProposal>('POST', `/api/ops/incidents/${alertId}/analyze`);
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '인시던트 분석 실패'); }
        return resp.data;
    }

    async approveResponse(proposalId: string, approved: boolean): Promise<{ status: string }> {
        const resp = await this.request<{ status: string }>(
            'POST', `/api/ops/responses/${proposalId}/approve`, { approved }
        );
        return { status: resp.data?.status ?? (resp.success ? 'executed' : 'error') };
    }

    async getCostSummary(): Promise<CostSummary> {
        const resp = await this.request<CostSummary>('GET', '/api/session/cost');
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '비용 정보 조회 실패'); }
        return resp.data;
    }

    async createProject(profile: ProjectProfile): Promise<ProjectProfile> {
        const resp = await this.request<ProjectProfile>('POST', '/api/projects', profile);
        if (!resp.success || !resp.data) { throw new Error(resp.error ?? '프로젝트 생성 실패'); }
        return resp.data;
    }

    async getProject(projectId: string): Promise<ProjectProfile | null> {
        const resp = await this.request<ProjectProfile>('GET', `/api/projects/${projectId}`);
        return resp.success && resp.data ? resp.data : null;
    }
}
