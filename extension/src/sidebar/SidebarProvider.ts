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

/**
 * Core 의 ReadyStatus enum 값("ok" | "partial" | "fail")을 Webview 가 기대하는
 * "ready" / "not_ready" 문자열로 정규화한다.
 *
 * Strict 정책: 실제 사용 가능한 OK 상태에서만 ✓ 표시.
 *   - "ok" / "ready"            → "ready"      (✓)
 *   - "partial" / "fail" / 그 외 → "not_ready"  (✗)
 *
 * PARTIAL 을 ✗ 로 처리하는 이유: 사용자가 실제로 그 기능을 호출했을 때 실패할
 * 수 있는 상태를 ✓ 로 속이면 안 됨. (예: Docker version 은 있지만 daemon 다운 →
 * docker build 하면 즉시 실패. AI 자격증명만 있고 invoke 실패 → analyze 즉시 실패.)
 */
function _normalizeReadyValue(v: unknown): string {
    if (typeof v !== 'string') { return 'not_ready'; }
    const s = v.toLowerCase().trim();
    if (s === 'ready' || s === 'ok') { return 'ready'; }
    return 'not_ready';
}

function _normalizeDiagnostics<T extends Record<string, unknown>>(d: T | null | undefined): T | null {
    if (!d) { return null; }
    const fields = [
        'core_ready',
        'ai_ready',
        'docker_ready',
        'aws_deploy_ready',
        'ops_ready',
        'git_ready',
        'github_ready',
    ];
    const out: Record<string, unknown> = { ...d };
    for (const f of fields) {
        if (f in out) {
            out[f] = _normalizeReadyValue(out[f]);
        }
    }
    return out as T;
}

export class SidebarProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'recoder.sidebarView';

    // 코드 에이전트 멀티턴: 직전 생성/적용 파일 보관
    private _lastCodeOps: Array<{ path: string; content: string }> = [];
    // diff 미리보기용 제안 콘텐츠 (scheme: recoder-codegen)
    private readonly _codegenDocs = new Map<string, string>();
    private _codegenProviderRegistered = false;
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

    /** 탐색기에서 폴더 우클릭 → '여기에 코드 생성' 시 webview 에 대상 폴더 주입. */
    setCodeTargetFolder(folder: string): void {
        this.postMessage('code.setTargetFolder', { folder });
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

    triggerGithubActionsGeneration(): void {
        const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        void this.handleGenerateGithubActions(workspacePath, undefined);
    }

    triggerDiagnostics(): void {
        void this.handleMessage({ type: 'runDiagnostics', payload: {} });
    }

    private async handleMessage(message: { type: string; payload: unknown }): Promise<void> {
        const { type, payload } = message;

        switch (type) {
            // ── Build mode (webview-src/components/BuildMode.tsx) ─────────────
            case 'build.analyzeActive': {
                // 자동 감지와 동일 경로: 최근 터미널 출력 + 에디터 선택을 모아 분석.
                await vscode.commands.executeCommand('recoder.analyzeError');
                break;
            }
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
            case 'webview.diagnostics.fix': {
                // DiagnosticsPanel.tsx 의 chip 별 "Retry check" 버튼.
                // not-ready 인 항목을 누르면 해당 설정 GUI 를 자동으로 띄운다.
                const { key } = (payload ?? {}) as { key?: string };
                switch (key) {
                    case 'aws_deploy_ready':
                    case 'ai_ready':  // Bedrock 도 AWS 자격증명을 사용
                        await vscode.commands.executeCommand('recoder.awsConfigure');
                        break;
                    case 'github_ready':
                        await vscode.commands.executeCommand('recoder.githubLogin');
                        break;
                    case 'docker_ready': {
                        // Docker Desktop 실행 안내만 — 자동 시작은 위험
                        const selected = await vscode.window.showInformationMessage(
                            'Docker Desktop 을 시작한 후 진단을 다시 실행해주세요.',
                            'Docker Desktop 다운로드',
                        );
                        if (selected === 'Docker Desktop 다운로드') {
                            void vscode.env.openExternal(
                                vscode.Uri.parse('https://www.docker.com/products/docker-desktop'),
                            );
                        }
                        void this.handleMessage({ type: 'runDiagnostics', payload: {} });
                        break;
                    }
                    case 'core_ready':
                        await vscode.commands.executeCommand('recoder.restartCore');
                        break;
                    case 'ops_ready':
                        // Operate 탭에서 EC2 host / SSH key 를 설정 — 모드만 전환
                        this._state.currentMode = Mode.OPERATE;
                        this.postMessage('stateUpdate', this._state);
                        break;
                    default:
                        // 알 수 없는 key — 그냥 진단 재실행
                        void this.handleMessage({ type: 'runDiagnostics', payload: {} });
                }
                break;
            }
            case 'webview.open.external': {
                const { url } = (payload ?? {}) as { url?: string };
                if (url) {
                    void vscode.env.openExternal(vscode.Uri.parse(url));
                }
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
            case 'generateGithubActions': {
                const { workspacePath: ghWs, projectId: ghPid } = payload as { workspacePath: string; projectId?: string };
                await this.handleGenerateGithubActions(ghWs, ghPid);
                break;
            }
            case 'approveGithubActions': {
                const { proposalId, approved } = payload as { proposalId: string; approved: boolean };
                const ghResult = await this._apiClient.approveGithubActions(proposalId, approved);
                this.postMessage('stateUpdate', { ...this._state });
                if (ghResult.status === 'error') {
                    this.postMessage('errorMessage', { message: 'GitHub Actions 승인 실패' });
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
                    // Core 가 ReadyStatus enum 값("ok"/"partial"/"fail") 으로 보내는데
                    // webview 는 "ready" 문자열로 비교. 여기서 변환.
                    const normalized = _normalizeDiagnostics(
                        diagnostics as unknown as Record<string, unknown>
                    ) as unknown as typeof diagnostics;
                    if (normalized) {
                        this._state.diagnostics = normalized;
                        this.postMessage('diagnosticsUpdate', normalized);
                    }
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
            // ── AWS Credentials / Status (§S-2 — /api/aws/* 라우트) ────────
            case 'aws.status': {
                try {
                    const status = await this._apiClient.getAwsStatus();
                    this.postMessage('aws.status', status);
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('aws.status', {
                        ready: false,
                        identity: null,
                        region: '',
                        profile: '',
                        access_key_last4: '',
                        storage: '',
                        message: msg,
                    });
                    this.postMessage('errorMessage', { message: `AWS 상태 조회 실패: ${msg}` });
                }
                break;
            }
            case 'aws.configure': {
                const p = (payload ?? {}) as {
                    accessKeyId?: string;
                    secretAccessKey?: string;
                    region?: string;
                    profile?: string;
                    storage?: 'recoder' | 'aws_credentials_file';
                    sessionToken?: string;
                };
                if (!p.accessKeyId || !p.secretAccessKey) {
                    this.postMessage('aws.configure.result', {
                        ok: false,
                        message: 'accessKeyId / secretAccessKey 가 비어있습니다.',
                    });
                    break;
                }
                try {
                    const status = await this._apiClient.configureAws({
                        accessKeyId: p.accessKeyId,
                        secretAccessKey: p.secretAccessKey,
                        region: p.region,
                        profile: p.profile,
                        storage: p.storage,
                        sessionToken: p.sessionToken,
                    });
                    this.postMessage('aws.configure.result', { ok: true, status });
                    this.postMessage('aws.status', status);
                    // 자격증명 저장 직후 diagnostics 도 즉시 갱신 (Core 가 캐시를 다시 작성하지만
                    // webview UI 의 readyCheck 까지 동기화시키기 위해 한번 더 트리거).
                    void this.handleMessage({ type: 'runDiagnostics', payload: {} });
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('aws.configure.result', { ok: false, message: msg });
                    this.postMessage('errorMessage', { message: `AWS 자격증명 등록 실패: ${msg}` });
                }
                break;
            }
            case 'aws.clear': {
                try {
                    await this._apiClient.clearAws();
                    this.postMessage('aws.clear.result', { ok: true });
                    // status / diagnostics 동시 갱신
                    try {
                        const status = await this._apiClient.getAwsStatus();
                        this.postMessage('aws.status', status);
                    } catch { /* ignore */ }
                    void this.handleMessage({ type: 'runDiagnostics', payload: {} });
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('aws.clear.result', { ok: false, message: msg });
                    this.postMessage('errorMessage', { message: `AWS 자격증명 제거 실패: ${msg}` });
                }
                break;
            }
            case 'aws.listProfiles': {
                try {
                    const profiles = await this._apiClient.listAwsProfiles();
                    this.postMessage('aws.profiles', { profiles });
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('aws.profiles', { profiles: [], error: msg });
                }
                break;
            }
            case 'aws.listEcrRepos': {
                const p = (payload ?? {}) as { region?: string; profile?: string; maxResults?: number };
                try {
                    const repositories = await this._apiClient.listEcrRepos({
                        region: p.region,
                        profile: p.profile,
                        maxResults: p.maxResults,
                    });
                    this.postMessage('aws.ecrRepos', { repositories });
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('aws.ecrRepos', { repositories: [], error: msg });
                }
                break;
            }
            case 'code.generate': {
                const p = (payload ?? {}) as {
                    instruction?: string;
                    openFile?: { path: string; content: string };
                    targetFolder?: string;
                    contextFiles?: Array<{ path: string; content: string }>;
                };
                await this.handleCodeGenerate(p.instruction ?? '', {
                    openFile: p.openFile,
                    targetFolder: p.targetFolder ?? '',
                    contextFiles: p.contextFiles ?? [],
                });
                break;
            }
            case 'code.apply': {
                const { file, content, targetFolder } = (payload ?? {}) as { file?: string; content?: string; targetFolder?: string };
                await this.handleCodeApply(file ?? '', content ?? '', targetFolder ?? '');
                break;
            }
            case 'code.applyAll': {
                const { ops, targetFolder } = (payload ?? {}) as { ops?: Array<{ file: string; content: string }>; targetFolder?: string };
                let okCount = 0;
                for (const op of ops ?? []) {
                    const ok = await this.handleCodeApply(op.file, op.content, targetFolder ?? '');
                    if (ok) { okCount++; }
                }
                this.postMessage('code.applied', { file: `전체 ${okCount}개`, ok: okCount > 0 });
                break;
            }
            case 'code.diff': {
                const { file, content, targetFolder } = (payload ?? {}) as { file?: string; content?: string; targetFolder?: string };
                await this.handleCodeDiff(this._joinFolder(targetFolder ?? '', file ?? ''), content ?? '');
                break;
            }
            case 'code.pickFolder': {
                const picked = await vscode.window.showOpenDialog({
                    canSelectFolders: true, canSelectFiles: false, canSelectMany: false,
                    openLabel: '이 폴더에 생성',
                    defaultUri: vscode.workspace.workspaceFolders?.[0]?.uri,
                });
                if (picked && picked[0]) {
                    const rel = vscode.workspace.asRelativePath(picked[0], false);
                    this.postMessage('code.folderPicked', { folder: rel === picked[0].fsPath ? '' : rel });
                }
                break;
            }
            case 'code.pickContext': {
                const picked = await vscode.window.showOpenDialog({
                    canSelectFolders: false, canSelectFiles: true, canSelectMany: true,
                    openLabel: '컨텍스트로 첨부',
                    defaultUri: vscode.workspace.workspaceFolders?.[0]?.uri,
                });
                const files: Array<{ path: string; content: string }> = [];
                for (const uri of picked ?? []) {
                    try {
                        const buf = await vscode.workspace.fs.readFile(uri);
                        let text = new TextDecoder().decode(buf);
                        if (text.length > 20000) { text = text.slice(0, 20000); }
                        files.push({ path: vscode.workspace.asRelativePath(uri), content: text });
                    } catch { /* skip */ }
                }
                if (files.length) { this.postMessage('code.contextAdded', { files }); }
                break;
            }
            default:
                console.warn('[SidebarProvider] Unknown message type:', type);
        }
    }

    /** EDIT op 의 제안 내용을 현재 파일과 나란히 diff 로 연다(적용 전 검토). */
    private async handleCodeDiff(file: string, content: string): Promise<void> {
        if (!this._codegenProviderRegistered) {
            const docs = this._codegenDocs;
            vscode.workspace.registerTextDocumentContentProvider('recoder-codegen', {
                provideTextDocumentContent(uri: vscode.Uri): string {
                    return docs.get(uri.path.replace(/^\//, '')) ?? '';
                },
            });
            this._codegenProviderRegistered = true;
        }
        const root = vscode.workspace.workspaceFolders?.[0]?.uri;
        if (!root) { this.postMessage('code.error', { message: '워크스페이스가 열려있지 않습니다.' }); return; }
        const safe = file.replace(/\\/g, '/').replace(/^\/+/, '').split('/').filter((seg) => seg && seg !== '..').join('/');
        if (!safe) { return; }
        const fileUri = vscode.Uri.joinPath(root, safe);
        this._codegenDocs.set(safe, content);
        const proposedUri = vscode.Uri.parse(`recoder-codegen:/${safe}`);
        try {
            const exists = await this._fileExists(fileUri);
            const leftUri = exists ? fileUri : vscode.Uri.parse(`recoder-codegen:/(빈 파일)`);
            if (!exists) { this._codegenDocs.set('(빈 파일)', ''); }
            await vscode.commands.executeCommand('vscode.diff', leftUri, proposedUri, `ReCoder 제안: ${safe}`);
        } catch (err) {
            this.postMessage('code.error', { message: `diff 열기 실패: ${err}` });
        }
    }

    private async _fileExists(uri: vscode.Uri): Promise<boolean> {
        try { await vscode.workspace.fs.stat(uri); return true; } catch { return false; }
    }

    /** 워크스페이스 상대 폴더 + 파일명을 안전하게 결합. */
    private _joinFolder(folder: string, file: string): string {
        const f = (folder || '').replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
        const n = (file || '').replace(/\\/g, '/').replace(/^\/+/, '');
        return f ? `${f}/${n}` : n;
    }

    /** 코드 생성 에이전트 — 자연어 요청을 Core 로 보내 ops 를 받아 webview 로 회신. */
    private async handleCodeGenerate(
        instruction: string,
        opts: { openFile?: { path: string; content: string }; targetFolder?: string; contextFiles?: Array<{ path: string; content: string }> } = {},
    ): Promise<void> {
        if (!instruction.trim()) { return; }
        const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        // 인자가 없으면 현재 활성 에디터를 컨텍스트로 자동 첨부.
        let attach = opts.openFile;
        if (!attach) {
            const ed = vscode.window.activeTextEditor;
            if (ed && !ed.document.isUntitled) {
                attach = { path: vscode.workspace.asRelativePath(ed.document.uri), content: ed.document.getText() };
            }
        }
        try {
            const result = await this._apiClient.generateCode(instruction, {
                workspacePath,
                openFile: attach,
                priorFiles: this._lastCodeOps,
                contextFiles: opts.contextFiles ?? [],
                targetFolder: opts.targetFolder ?? '',
            });
            // 다음 턴 컨텍스트로 보관
            this._lastCodeOps = (result.ops ?? []).map((op) => ({ path: op.file, content: op.content }));
            this.postMessage('code.result', result);
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            this.postMessage('code.error', { message: msg });
        }
    }

    /** 단일 파일 op 적용 — 워크스페이스에 직접 쓰고 에디터로 연다(Codex 식). 반환: 성공 여부. */
    private async handleCodeApply(file: string, content: string, targetFolder: string = ''): Promise<boolean> {
        const root = vscode.workspace.workspaceFolders?.[0]?.uri;
        if (!root) {
            this.postMessage('code.error', { message: '워크스페이스가 열려있지 않습니다.' });
            return false;
        }
        const combined = this._joinFolder(targetFolder, file);
        const safe = combined.replace(/\\/g, '/').replace(/^\/+/, '').split('/').filter((seg) => seg && seg !== '..').join('/');
        if (!safe) {
            this.postMessage('code.error', { message: `잘못된 파일명: ${file}` });
            return false;
        }
        const uri = vscode.Uri.joinPath(root, safe);
        try {
            await vscode.workspace.fs.createDirectory(vscode.Uri.joinPath(uri, '..'));
            await vscode.workspace.fs.writeFile(uri, new TextEncoder().encode(content));
            await vscode.window.showTextDocument(uri, { preview: false });
            this.postMessage('code.applied', { file: safe, ok: true });
            return true;
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            this.postMessage('code.error', { message: `${safe} 적용 실패: ${msg}` });
            return false;
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

    private async handleGenerateGithubActions(workspacePath: string, projectId?: string): Promise<void> {
        try {
            this._state.isLoading = true;
            this.postMessage('stateUpdate', this._state);
            const proposal = await this._apiClient.generateGithubActions(workspacePath, projectId);
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
