import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import {
    SidebarState,
    Mode,
    AnalyzeRequest,
    PatchProposal,
    InfraFileProposal,
    ResponseProposal,
    CoreHealth,
    DeployMethod,
} from '../types';
import { ApiClient } from '../core/ApiClient';
import { CoreManager } from '../core/CoreManager';
import { PollingService } from '../core/PollingService';

export class SidebarProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'recoder.sidebarView';
    private _view?: vscode.WebviewView;
    private _state: SidebarState;

    constructor(
        private readonly _extensionUri: vscode.Uri,
        private readonly _apiClient: ApiClient,
        private readonly _coreManager: CoreManager,
        private readonly _pollingService: PollingService
    ) {
        this._state = { currentMode: Mode.BUILD, proposals: [], isLoading: false };
    }

    resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken
    ): void {
        this._view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.joinPath(this._extensionUri, 'media'),
                vscode.Uri.joinPath(this._extensionUri, 'out'),
                vscode.Uri.joinPath(this._extensionUri, 'webview-ui', 'dist'),
            ],
        };

        webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);

        webviewView.webview.onDidReceiveMessage(
            (message: { type: string; payload: unknown }) => {
                void this.handleMessage(message);
            }
        );

        // ensureRunning을 먼저 끝낸 뒤 polling 을 시작한다.
        // 그렇지 않으면 첫 polling 호출이 세션 토큰 채워지기 전에 발생해 401 이 나고
        // 사이드바가 "연결 중…" 상태로 멈춘다.
        void (async () => {
            let coreOk = false;
            try {
                await this._coreManager.ensureRunning();
                // ensureRunning 이후에도 토큰이 비어있을 수 있으니 강제 refresh.
                await this._coreManager.refreshToken();
                coreOk = true;
            } catch (err) {
                const message = err instanceof Error ? err.message : String(err);
                this.postMessage('errorMessage', { message: `Core 시작 실패: ${message}` });
            } finally {
                // ensureRunning 성공/실패와 무관하게 polling 시작 (실패 시에도 down 상태 표시)
                this._pollingService.start(
                    (health: CoreHealth) => {
                        this.postMessage('healthUpdate', health);
                        if (health.status === 'ok') { void this.refreshCost(); }
                    },
                    (err: Error) => {
                        this.postMessage('errorMessage', { message: err.message });
                    }
                );
            }
            // Core 가 떴으면 진단을 자동으로 1회 돌려준다. (App.tsx 가 mount 시 요청을 보내지만
            // 그 때 토큰이 아직 비어있을 수 있어 401 이 나는 경우가 있어 한 번 더 트리거.)
            if (coreOk) {
                void this.handleMessage({ type: 'runDiagnostics', payload: {} });
            }
        })();

        this.postMessage('stateUpdate', this._state);

        webviewView.onDidChangeVisibility(() => {
            if (webviewView.visible) {
                this._pollingService.start(
                    (h) => this.postMessage('healthUpdate', h),
                    (e) => this.postMessage('errorMessage', { message: e.message })
                );
            } else {
                this._pollingService.stop();
            }
        });

        webviewView.onDidDispose(() => { this._pollingService.stop(); });
    }

    postMessage(type: string, payload: unknown): void {
        if (this._view) { void this._view.webview.postMessage({ type, payload }); }
    }

    triggerAnalysis(request: Partial<AnalyzeRequest>): void {
        this.postMessage('core.analyzeStarted', {});
        void this.handleAnalyze(request);
    }

    switchToShipMode(): void {
        this._state.currentMode = Mode.SHIP;
        this.postMessage('stateUpdate', this._state);
    }

    triggerDockerfileGeneration(): void {
        const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        void this.handleGenerateDockerfile(workspacePath, undefined);
    }

    triggerDiagnostics(): void {
        void this.handleMessage({ type: 'runDiagnostics', payload: {} });
    }

    private async handleMessage(message: { type: string; payload: unknown }): Promise<void> {
        const { type, payload } = message;

        switch (type) {
            // ── Build mode (webview-src/components/BuildMode.tsx) ─────────────
            case 'analyze':
            case 'build.analyze': {
                // BuildMode.tsx 가 보내는 형태: { error_log: string }
                // 기존 형태: Partial<AnalyzeRequest>
                const p = (payload ?? {}) as { error_log?: string } & Partial<AnalyzeRequest>;
                if (p.error_log && !p.terminal_output) {
                    p.terminal_output = p.error_log;
                }
                await this.handleAnalyze(p);
                break;
            }
            case 'build.patch.approve': {
                const { proposal_id } = (payload ?? {}) as { proposal_id: string };
                await this.handleApprovePatch(proposal_id, true);
                break;
            }
            case 'build.patch.reject': {
                const { proposal_id } = (payload ?? {}) as { proposal_id: string };
                await this.handleApprovePatch(proposal_id, false);
                break;
            }
            case 'webview.paste.request': {
                // BuildMode 가 클립보드 텍스트 요청. Extension 측에서 읽어 다시 전달.
                try {
                    const text = await vscode.env.clipboard.readText();
                    this.postMessage('webview.paste.response', { text });
                } catch (err) {
                    this.postMessage('errorMessage', { message: String(err) });
                }
                break;
            }
            case 'webview.diagnostics.rerun': {
                // DiagnosticsPanel.tsx 의 "다시 진단" 버튼.
                void this.handleMessage({ type: 'runDiagnostics', payload: {} });
                break;
            }
            case 'workbench.open':
            case 'openWorkbench': {
                await vscode.commands.executeCommand('recoder.openWorkbench');
                break;
            }
            case 'approvePatch': {
                const { proposalId, approved } = payload as { proposalId: string; approved: boolean };
                await this.handleApprovePatch(proposalId, approved);
                break;
            }
            case 'pasteErrorLog': {
                await this.handlePasteErrorLog(payload as string);
                break;
            }
            case 'generateDockerfile': {
                const { workspacePath, projectId } = payload as { workspacePath: string; projectId: string };
                await this.handleGenerateDockerfile(workspacePath, projectId);
                break;
            }
            case 'approveDockerfile': {
                const { proposalId, approved } = payload as { proposalId: string; approved: boolean };
                const dfResult = await this._apiClient.approveDockerfile(proposalId, approved);
                this.postMessage('stateUpdate', { ...this._state });
                if (dfResult.status === 'error') {
                    this.postMessage('errorMessage', { message: 'Dockerfile 승인 실패' });
                }
                break;
            }
            case 'runScan': {
                const { scanType, workspacePath: scanWs, targetPath } = payload as {
                    scanType: 'trivy' | 'hadolint' | 'gitleaks';
                    workspacePath: string;
                    targetPath?: string;
                };
                try {
                    const scanResult = await this._apiClient.runScan(scanType, scanWs, targetPath);
                    this.postMessage('scanResult', scanResult);
                } catch (err) {
                    this.postMessage('errorMessage', { message: String(err) });
                }
                break;
            }
            case 'createDeployPlan': {
                const {
                    workspacePath: dpWs, method: dpMethod, projectId: dpPid,
                    image: dpImg, containerName: dpCn, hostPort: dpHp, containerPort: dpCp
                } = payload as {
                    workspacePath: string; method: DeployMethod; projectId?: string;
                    image?: string; containerName?: string; hostPort?: number; containerPort?: number;
                };
                try {
                    const plan = await this._apiClient.createDeploymentPlan(
                        dpWs, dpMethod, dpPid, dpImg, dpCn, dpHp, dpCp
                    );
                    this.postMessage('proposalReady', plan);
                } catch (err) {
                    this.postMessage('errorMessage', { message: String(err) });
                }
                break;
            }
            case 'executeDeployment': {
                const { planId, approved } = payload as { planId: string; approved: boolean };
                try {
                    const deployResult = await this._apiClient.executeDeployment(planId, approved);
                    this.postMessage('deployResult', deployResult);
                } catch (err) {
                    this.postMessage('errorMessage', { message: String(err) });
                }
                break;
            }
            case 'rollback': {
                const { deploymentId } = payload as { deploymentId: string };
                try {
                    const result = await this._apiClient.rollback(deploymentId);
                    this.postMessage('stateUpdate', { rollbackResult: result, ...this._state });
                } catch (err) {
                    this.postMessage('errorMessage', { message: String(err) });
                }
                break;
            }
            case 'fetchIncidents': {
                const { host, sshKeyPath } = payload as { host: string; sshKeyPath: string };
                try {
                    const incidents = await this._apiClient.fetchIncidents(host, sshKeyPath);
                    this.postMessage('stateUpdate', { incidents, ...this._state });
                } catch (err) {
                    this.postMessage('errorMessage', { message: String(err) });
                }
                break;
            }
            case 'analyzeIncident': {
                const { alertId } = payload as { alertId: string };
                try {
                    const proposal = await this._apiClient.analyzeIncident(alertId);
                    this._state.proposals.push(proposal);
                    this.postMessage('proposalReady', proposal);
                } catch (err) {
                    this.postMessage('errorMessage', { message: String(err) });
                }
                break;
            }
            case 'approveResponse': {
                const { proposalId, approved } = payload as { proposalId: string; approved: boolean };
                const opsResult = await this._apiClient.approveResponse(proposalId, approved);
                if (opsResult.status === 'error') {
                    this.postMessage('errorMessage', { message: '응답 승인 실패' });
                } else {
                    // Remove the ResponseProposal from state (matched by proposal_id)
                    this._state.proposals = this._state.proposals.filter(
                        (p) => (p as import('../types').ResponseProposal).proposal_id !== proposalId
                    );
                    this.postMessage('opsResult', opsResult);
                    this.postMessage('stateUpdate', this._state);
                }
                break;
            }
            case 'runDiagnostics': {
                this._state.isLoading = true;
                this.postMessage('stateUpdate', this._state);
                let diagnostics: import('../types').DiagnosticsResult | null = null;
                try {
                    diagnostics = await this._apiClient.runDiagnostics();
                } catch (err) {
                    // POST /api/diagnostics/run 실패 — 캐시된 결과(GET) 로 fallback.
                    try { diagnostics = await this._apiClient.getDiagnostics(); } catch { /* ignore */ }
                    if (!diagnostics) {
                        this.postMessage('errorMessage', { message: `진단 실행 실패: ${String(err)}` });
                    }
                }
                this._state.isLoading = false;
                if (diagnostics) {
                    this._state.diagnostics = diagnostics;
                    this.postMessage('diagnosticsUpdate', diagnostics);
                }
                // 성공·실패와 무관하게 stateUpdate 을 보내 isLoading 스피너 해제.
                this.postMessage('stateUpdate', this._state);
                break;
            }
            case 'switchMode': {
                const { mode } = payload as { mode: Mode };
                this._state.currentMode = mode;
                this.postMessage('stateUpdate', this._state);
                break;
            }
            // ── §38 Deploy Replay ──────────────────────────────────────────
            case 'loadReplay': {
                // webview Replay.tsx 가 보내는 형태: { deployId: string, service?, cluster?, region?, windowHours? }
                const p = (payload ?? {}) as {
                    deployId?: string;
                    service?: string;
                    cluster?: string;
                    region?: string;
                    windowHours?: number;
                };
                const deployId = (p.deployId ?? '').trim();
                if (!deployId) {
                    this.postMessage('replayTimeline', {
                        error: 'deployId 가 비어있습니다.',
                    });
                    break;
                }
                try {
                    const timeline = await this._apiClient.loadReplayTimeline(deployId, {
                        service: p.service,
                        cluster: p.cluster,
                        region: p.region,
                        windowHours: p.windowHours,
                    });
                    if (timeline) {
                        this.postMessage('replayTimeline', timeline);
                    } else {
                        this.postMessage('replayTimeline', {
                            error: 'Core 가 타임라인을 반환하지 않았습니다.',
                        });
                    }
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('replayTimeline', { error: msg });
                }
                break;
            }
            // ------------------------------------------------------------------
            // Webview polling requests — webview asks for latest data
            // ------------------------------------------------------------------
            case 'webview.poll.health': {
                // Respond with last known health from PollingService
                const lastHealth = this._pollingService.getLastHealth();
                if (lastHealth) {
                    this.postMessage('healthUpdate', lastHealth);
                } else {
                    // Trigger a fresh poll
                    void this._pollingService.poll();
                }
                break;
            }
            case 'webview.poll.cost': {
                void this.refreshCost();
                break;
            }
            case 'webview.poll.status': {
                void this._pollingService.poll();
                break;
            }
            case 'webview.ready': {
                // Webview finished loading — send current state immediately
                this.postMessage('stateUpdate', this._state);
                const health = this._pollingService.getLastHealth();
                if (health) { this.postMessage('healthUpdate', health); }
                if (this._state.costSummary) { this.postMessage('costUpdate', this._state.costSummary); }
                break;
            }
            default:
                console.warn('[SidebarProvider] Unknown message type:', type);
        }
    }

    private async handleAnalyze(partialRequest: Partial<AnalyzeRequest>): Promise<void> {
        this._state.isLoading = true;
        this.postMessage('stateUpdate', this._state);
        try {
            const context = await this.collectContext();
            const request: AnalyzeRequest = {
                workspace_path: context.workspace_path ?? '',
                ...context,
                ...partialRequest,
            };
            const proposal = await this._apiClient.analyze(request);
            if (proposal) {
                this._state.proposals.unshift(proposal);
                this.postMessage('proposalReady', proposal);
                // BuildMode.tsx 가 기다리는 별칭 메시지. 같은 payload.
                this.postMessage('build.analysis.result', proposal);
            } else {
                // Core 가 null 을 돌려준 경우도 BuildMode 스피너를 풀어줘야 함.
                const msg = '분석 결과를 받지 못했습니다. (Core 응답 비어있음 — 토큰/네트워크/LLM 키 확인)';
                this.postMessage('errorMessage', { message: msg });
                this.postMessage('build.analysis.error', msg);
            }
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            this.postMessage('errorMessage', { message: msg });
            this.postMessage('build.analysis.error', msg);
        } finally {
            this._state.isLoading = false;
            this.postMessage('stateUpdate', this._state);
        }
    }

    private async handleApprovePatch(proposalId: string, approved: boolean): Promise<void> {
        try {
            const result = await this._apiClient.approvePatch(proposalId, approved);
            if (result.status === 'applied' || result.status === 'rejected') {
                this._state.proposals = this._state.proposals.filter(
                    (p) => (p as PatchProposal).proposal_id !== proposalId
                );
                this.postMessage('patchResult', { status: result.status, proposalId });
                this.postMessage('stateUpdate', this._state);
                // BuildMode.tsx 가 기다리는 별칭 메시지.
                this.postMessage(
                    result.status === 'applied' ? 'build.patch.applied' : 'build.patch.rejected',
                    { proposal_id: proposalId }
                );
            } else {
                const msg = '패치 승인 처리 실패';
                this.postMessage('errorMessage', { message: msg });
                this.postMessage('build.analysis.error', msg);
            }
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            this.postMessage('errorMessage', { message: msg });
            this.postMessage('build.analysis.error', msg);
        }
    }

    private async handlePasteErrorLog(errorLog: string): Promise<void> {
        const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        await this.handleAnalyze({ workspace_path: workspacePath, terminal_output: errorLog });
    }

    private async handleGenerateDockerfile(workspacePath: string, projectId?: string): Promise<void> {
        try {
            this._state.isLoading = true;
            this.postMessage('stateUpdate', this._state);
            const proposal = await this._apiClient.generateDockerfile(workspacePath, projectId);
            this._state.proposals.unshift(proposal);
            this.postMessage('proposalReady', proposal);
        } catch (err) {
            this.postMessage('errorMessage', { message: String(err) });
        } finally {
            this._state.isLoading = false;
            this.postMessage('stateUpdate', this._state);
        }
    }

    private async refreshCost(): Promise<void> {
        try {
            const costSummary = await this._apiClient.getCostSummary();
            this._state.costSummary = costSummary;
            this.postMessage('costUpdate', costSummary);
        } catch { /* ignore */ }
    }

    private async collectContext(): Promise<Partial<AnalyzeRequest>> {
        const editor = vscode.window.activeTextEditor;
        const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        const result: Partial<AnalyzeRequest> = { workspace_path: workspacePath };
        if (editor) {
            result.active_file_path = editor.document.uri.fsPath;
            const selection = editor.selection;
            if (!selection.isEmpty) { result.selected_text = editor.document.getText(selection); }
        }
        result.project_files_summary = await this.buildProjectFilesSummary(workspacePath);
        return result;
    }

    private async buildProjectFilesSummary(workspacePath: string): Promise<string> {
        if (!workspacePath) { return ''; }
        const importantFiles = [
            'package.json', 'requirements.txt', 'pyproject.toml', 'go.mod',
            'pom.xml', 'build.gradle', 'Dockerfile', 'docker-compose.yml',
            'docker-compose.yaml', '.env.example', 'README.md',
        ];
        const found: string[] = [];
        for (const file of importantFiles) {
            if (fs.existsSync(path.join(workspacePath, file))) { found.push(file); }
        }
        return found.join(', ');
    }

    private _getHtmlForWebview(webview: vscode.Webview): string {
        const nonce = this.getNonce();
        const scriptUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'out', 'webview', 'webview.js')
        );
        const cspConnect = Array.from({ length: 17 }, (_, i) => `http://127.0.0.1:${17894 + i}`).join(' ');

        return `<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta http-equiv="Content-Security-Policy"
        content="default-src 'none';
                 style-src 'unsafe-inline';
                 script-src 'nonce-${nonce}';
                 img-src ${webview.cspSource} https: data:;
                 connect-src ${cspConnect};" />
    <title>ReCoder</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: var(--vscode-font-family, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif);
            font-size: var(--vscode-font-size, 13px);
            color: var(--vscode-editor-foreground, #ccc);
            background: var(--vscode-sideBar-background, #252526);
        }
        #root { padding: 0; }
    </style>
</head>
<body>
    <div id="root"></div>
    <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
    }

    private getNonce(): string {
        let text = '';
        const possible = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
        for (let i = 0; i < 32; i++) {
            text += possible.charAt(Math.floor(Math.random() * possible.length));
        }
        return text;
    }

    getState(): SidebarState { return this._state; }

    updateState(partial: Partial<SidebarState>): void {
        this._state = { ...this._state, ...partial };
        this.postMessage('stateUpdate', this._state);
    }
}
