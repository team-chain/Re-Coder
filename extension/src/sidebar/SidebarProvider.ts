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
import { ApiClient, CodeDecisionChoice } from '../core/ApiClient';
import { CoreManager } from '../core/CoreManager';
import { PollingService } from '../core/PollingService';
import { analyzeProject, analyzeFile } from '../codemap/analyzer';

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
    /** Editor Area 의 ReCoder 작업 화면. 사이드바와 같은 상태/메시지를 공유한다. */
    private _workspacePanelWebview?: vscode.Webview;
    private _workspaceAutoOpenRequested = false;
    // 구조 지도 자동 갱신: 파일 변경 감시 + 디바운스
    private _mapWatcher?: vscode.FileSystemWatcher;
    private _mapRefreshTimer?: ReturnType<typeof setTimeout>;
    private _state: SidebarState;

    constructor(
        private readonly _extensionUri: vscode.Uri,
        private readonly _apiClient: ApiClient,
        private readonly _coreManager: CoreManager,
        private readonly _pollingService: PollingService,
        private readonly _openWorkspace?: () => void,
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
                void this.handleMessage(message, webviewView.webview);
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

        // 파일이 생기거나 바뀌면 구조 지도를 자동으로 다시 그린다(서버 불필요).
        this._setupMapWatcher();

        webviewView.onDidChangeVisibility(() => {
            if (webviewView.visible) {
                this._openWorkspaceFromSidebar();
                this._pollingService.start(
                    (h) => this.postMessage('healthUpdate', h),
                    (e) => this.postMessage('errorMessage', { message: e.message })
                );
            } else if (!this._workspacePanelWebview) {
                this._pollingService.stop();
            }
        });

        // Activity Bar 의 ReCoder 아이콘을 눌러 처음 View가 resolve되는 경우에는
        // onDidChangeVisibility 이벤트가 이미 지나갈 수 있다. 다음 이벤트 루프에서
        // 명령 등록 완료 후 무조건 Workspace를 열어 사이드바가 남지 않게 한다.
        setTimeout(() => this._openWorkspaceFromSidebar(), 100);

        webviewView.onDidDispose(() => {
            if (this._view === webviewView) {
                this._view = undefined;
            }
            if (!this._workspacePanelWebview) {
                this._pollingService.stop();
            }
            this._mapWatcher?.dispose();
            this._mapWatcher = undefined;
            if (this._mapRefreshTimer) { clearTimeout(this._mapRefreshTimer); }
        });
    }

    postMessage(type: string, payload: unknown): void {
        if (this._view) { void this._view.webview.postMessage({ type, payload }); }
        if (this._workspacePanelWebview) {
            void this._workspacePanelWebview.postMessage({ type, payload });
        }
    }

    /** 요청-응답 메시지는 요청을 보낸 Webview로만 돌려보낸다. */
    private postMessageToWebview(webview: vscode.Webview | undefined, type: string, payload: unknown): void {
        if (webview) {
            void webview.postMessage({ type, payload });
            return;
        }
        // VS Code 명령처럼 Webview 밖에서 시작한 기존 호출은 전체에 알린다.
        this.postMessage(type, payload);
    }

    /**
     * 큰 작업 화면이 사이드바와 동일한 React 앱을 사용할 수 있도록 연결한다.
     * 메시지는 기존 핸들러로 라우팅되므로 코드 생성/승인/진단 흐름은 그대로 유지된다.
     */
    attachWorkspacePanel(webview: vscode.Webview): void {
        this._workspacePanelWebview = webview;
        webview.onDidReceiveMessage((message: { type: string; payload: unknown }) => {
            void this.handleMessage(message, webview);
        });
        this.postMessage('stateUpdate', this._state);
        // Core 시작은 명령 진입점에서 한 번만 수행한다. 여기서도 시작하면
        // Activity Bar 클릭 시 Sidebar/Panel 이 동시에 spawn 하며 로딩이 길어진다.
        this._pollingService.start(
            (health: CoreHealth) => {
                this.postMessage('healthUpdate', health);
                if (health.status === 'ok') { void this.refreshCost(); }
            },
            (err: Error) => this.postMessage('errorMessage', { message: err.message }),
        );
    }

    detachWorkspacePanel(webview: vscode.Webview): void {
        if (this._workspacePanelWebview === webview) {
            this._workspacePanelWebview = undefined;
            this._workspaceAutoOpenRequested = false;
            if (!this._view?.visible) {
                this._pollingService.stop();
            }
        }
    }

    private _openWorkspaceFromSidebar(): void {
        if (this._workspaceAutoOpenRequested || !this._openWorkspace) { return; }
        this._workspaceAutoOpenRequested = true;
        this._openWorkspace();
    }

    /** 워크스페이스 파일 변경을 감시해 구조 지도를 자동 갱신한다(생성/삭제/수정). */
    private _setupMapWatcher(): void {
        if (this._mapWatcher) { return; }
        const folder = vscode.workspace.workspaceFolders?.[0];
        if (!folder) { return; }
        const pattern = new vscode.RelativePattern(folder, '**/*.{py,js,mjs,jsx,ts,tsx,html,htm,css}');
        const watcher = vscode.workspace.createFileSystemWatcher(pattern);
        const SKIP = ['node_modules/', '.git/', '.venv/', 'venv/', 'out/', 'dist/', 'build/', 'site-packages/', '__pycache__/', '.next/', 'coverage/', '.aws-sam/'];
        const onFsEvent = (uri: vscode.Uri) => {
            const rel = vscode.workspace.asRelativePath(uri, false).replace(/\\/g, '/');
            if (SKIP.some((d) => rel.startsWith(d) || rel.includes('/' + d))) { return; }
            this._scheduleMapRefresh();
        };
        watcher.onDidCreate(onFsEvent);
        watcher.onDidDelete(onFsEvent);
        watcher.onDidChange(onFsEvent);
        this._mapWatcher = watcher;
    }

    /** 연속 변경을 모아 0.6초 뒤 한 번만 재분석(저장 폭주 방지). */
    private _scheduleMapRefresh(): void {
        if (this._mapRefreshTimer) { clearTimeout(this._mapRefreshTimer); }
        this._mapRefreshTimer = setTimeout(() => { this._doMapRefresh(); }, 600);
    }

    /** 사이드바가 보일 때만 재분석해 webview 에 푸시(파일 뷰면 화면을 빼앗지 않음). */
    private _doMapRefresh(): void {
        try {
            if (!this._view?.visible) { return; }
            const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
            if (!root) { return; }
            const data = analyzeProject(root);
            this.postMessage('map.projectResult', data);
        } catch { /* 무해하게 무시 */ }
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

    private async handleMessage(
        message: { type: string; payload: unknown },
        requestWebview?: vscode.Webview,
    ): Promise<void> {
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
            case 'sidebar.visible': {
                // retainContextWhenHidden 때문에 VS Code의 view visibility 이벤트가
                // 누락되는 경우에도 웹뷰가 다시 보였다는 신호로 Workspace를 연다.
                this._openWorkspaceFromSidebar();
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
                await this.handleGenerateDockerfile(
                    workspacePath || (vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? ''),
                    projectId,
                );
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
                await this.handleGenerateGithubActions(
                    ghWs || (vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? ''),
                    ghPid,
                );
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
            // ── 큰 ReCoder Workspace 의 배포 센터 ──────────────────────────
            case 'workspace.deploy.ec2': {
                const req = (payload ?? {}) as Parameters<ApiClient['deployEc2']>[0];
                try {
                    const result = await this._apiClient.deployEc2({
                        ...req,
                        workspace_path: req.workspace_path || (vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? ''),
                    });
                    this.postMessage('workspace.deploy.result', result);
                } catch (err) {
                    this.postMessage('errorMessage', { message: String(err) });
                }
                break;
            }
            case 'workspace.deploy.ecs': {
                const req = (payload ?? {}) as Parameters<ApiClient['deployEcs']>[0];
                try {
                    const result = await this._apiClient.deployEcs({
                        ...req,
                        workspace_path: req.workspace_path || (vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? ''),
                    });
                    this.postMessage('workspace.deploy.result', result);
                } catch (err) {
                    this.postMessage('errorMessage', { message: String(err) });
                }
                break;
            }
            case 'workspace.deploy.ec2.status': {
                try {
                    const status = await this._apiClient.getEc2DeployStatus();
                    this.postMessage('workspace.deploy.result', { message: status.error || `EC2: ${status.stage}` });
                } catch { /* status polling is best effort */ }
                break;
            }
            case 'workspace.deploy.ecs.status': {
                try {
                    const status = await this._apiClient.getEcsDeployStatus();
                    // 상태 폴링은 요청한 큰 창에만 돌려준다. 특히 롤백 제안은
                    // 다른 웹뷰의 배포에 붙으면 안 된다.
                    this.postMessageToWebview(requestWebview, 'workspace.deploy.ecs.statusResult', status);
                    // 배경 감시는 카드 정보만 갱신한다. 현재 ECS 탭에서
                    // 진행 상황을 보고 있을 때만 일반 상태 문구를 바꾼다.
                    const reportProgress = Boolean((payload as { reportProgress?: boolean } | undefined)?.reportProgress);
                    if (reportProgress) {
                        this.postMessageToWebview(requestWebview, 'workspace.deploy.result', {
                            message: status.error || `ECS: ${status.stage}`,
                        });
                    }
                } catch { /* status polling is best effort */ }
                break;
            }
            case 'workspace.deploy.ecs.rollback': {
                const p = (payload ?? {}) as { proposalId?: string; approved?: boolean };
                if (!p.proposalId || typeof p.approved !== 'boolean') {
                    this.postMessageToWebview(requestWebview, 'workspace.deploy.ecs.rollbackError', {
                        message: '롤백 제안 또는 승인 여부가 올바르지 않습니다.',
                    });
                    break;
                }
                try {
                    const result = await this._apiClient.resolveEcsRollback(p.proposalId, p.approved);
                    // 사용자 결정은 결과와 함께 ADR로 남긴다. AWS 호출 자체는
                    // Core가 끝낸 뒤이므로, 기록 저장 실패가 롤백 결과를 뒤집지
                    // 않도록 별도로 처리한다.
                    let adrPath = '';
                    let adrWarning = '';
                    try {
                        adrPath = await this.writeWorkspaceFile(result.adr.file, result.adr.content);
                    } catch (err) {
                        adrWarning = ` ADR 기록 저장 실패: ${err instanceof Error ? err.message : String(err)}`;
                    }
                    this.postMessageToWebview(requestWebview, 'workspace.deploy.ecs.rollbackResult', {
                        ...result,
                        adr_path: adrPath,
                        message: `${result.message}${adrWarning}`,
                    });
                } catch (err) {
                    const message = err instanceof Error ? err.message : String(err);
                    this.postMessageToWebview(requestWebview, 'workspace.deploy.ecs.rollbackError', { message });
                }
                break;
            }
            case 'workspace.deploy.preflight': {
                const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
                try {
                    const result = await this._apiClient.getDeployPreflight(workspacePath);
                    this.postMessageToWebview(requestWebview, 'workspace.deploy.preflightResult', result);
                } catch (err) {
                    const message = err instanceof Error ? err.message : String(err);
                    this.postMessageToWebview(requestWebview, 'workspace.deploy.preflightError', { message });
                }
                break;
            }
            case 'workspace.deploy.remediation.apply': {
                const proposalId = (payload as { proposalId?: string } | undefined)?.proposalId;
                const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
                if (!proposalId) {
                    this.postMessageToWebview(requestWebview, 'workspace.deploy.remediationError', {
                        message: '적용할 수정안 정보가 없습니다. 다시 검사해 주세요.',
                    });
                    break;
                }
                try {
                    const result = await this._apiClient.applyDeployRemediation(proposalId, workspacePath);
                    this.postMessageToWebview(requestWebview, 'workspace.deploy.remediationResult', result);
                } catch (err) {
                    const message = err instanceof Error ? err.message : String(err);
                    this.postMessageToWebview(requestWebview, 'workspace.deploy.remediationError', { message });
                }
                break;
            }
            case 'workspace.deploy.chooseTarget': {
                const p = (payload ?? {}) as {
                    target?: 'ecs' | 's3' | 'local';
                    evidence?: string[];
                };
                if (p.target !== 'ecs' && p.target !== 's3' && p.target !== 'local') {
                    this.postMessageToWebview(requestWebview, 'workspace.deploy.decisionError', {
                        message: '유효하지 않은 배포 대상입니다.',
                    });
                    break;
                }
                const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
                try {
                    const result = await this._apiClient.recordDeploymentDecision(
                        workspacePath,
                        p.target,
                        p.evidence ?? [],
                    );
                    const adrPath = await this.writeWorkspaceFile(result.adr.file, result.adr.content);
                    this.postMessageToWebview(requestWebview, 'workspace.deploy.decisionResult', {
                        ...result,
                        adr_path: adrPath,
                    });
                } catch (err) {
                    const message = err instanceof Error ? err.message : String(err);
                    this.postMessageToWebview(requestWebview, 'workspace.deploy.decisionError', { message });
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
                    const actorToken = await vscode.window.showInputBox({
                        prompt: '1차 Ops 승인자 자격증명을 입력하세요',
                        password: true,
                        ignoreFocusOut: true,
                    });
                    if (!actorToken) { break; }
                    const proposal = await this._apiClient.analyzeIncident(alertId, actorToken);
                    this._state.proposals.push(proposal);
                    this.postMessage('proposalReady', proposal);
                } catch (err) {
                    this.postMessage('errorMessage', { message: String(err) });
                }
                break;
            }
            case 'approveResponse': {
                const { proposalId, approved } = payload as { proposalId: string; approved: boolean };
                const proposal = this._state.proposals.find(
                    (p) => (p as ResponseProposal).proposal_id === proposalId
                ) as ResponseProposal | undefined;
                const actorToken = await vscode.window.showInputBox({
                    prompt: approved
                        ? '2차 Ops 승인자 자격증명을 입력하세요'
                        : '거부 처리할 Ops 승인자 자격증명을 입력하세요',
                    password: true,
                    ignoreFocusOut: true,
                });
                if (!actorToken) { break; }
                const opsResult = await this._apiClient.approveResponse(
                    proposalId,
                    approved,
                    undefined,
                    undefined,
                    undefined,
                    actorToken,
                    proposal?.confirm_token,
                );
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
                    // Core는 STS 검증만 한다. 검증 후 키의 영속 보관은 OS 보안
                    // 저장소(SecretStorage)에서만 이루어져 파일에 평문이 남지 않는다.
                    const status = await this._apiClient.connectAws({
                        accessKeyId: p.accessKeyId,
                        secretAccessKey: p.secretAccessKey,
                        region: p.region,
                        sessionToken: p.sessionToken,
                    });
                    await this._coreManager.storeAwsCredentials({
                        accessKeyId: p.accessKeyId,
                        secretAccessKey: p.secretAccessKey,
                        region: p.region?.trim() || 'ap-northeast-2',
                        sessionToken: p.sessionToken,
                    });
                    // /api/aws/connect는 검증을 통과한 키를 현재 Core 메모리에만
                    // 적용한다. 재시작은 다음 Core 시작 시 SecretStorage 주입으로
                    // 처리하므로, 여기서 Core를 죽여 연결 직후 상태가 사라지지 않게 한다.
                    // connect 응답에는 이번 등록에서 수행한 권한 점검 결과가 들어 있다.
                    // 여기서 status API로 다시 덮어쓰면 permission_check가 사라져
                    // 등록 직후 경고가 보이지 않으므로, 검증 응답을 그대로 전달한다.
                    this.postMessage('aws.configure.result', { ok: true, status });
                    this.postMessage('aws.status', status);
                    void this.handleMessage({ type: 'runDiagnostics', payload: {} });
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('aws.configure.result', { ok: false, message: msg });
                    this.postMessage('errorMessage', { message: `AWS 자격증명 등록 실패: ${msg}` });
                }
                break;
            }
            case 'aws.permissions.check': {
                try {
                    const p = (payload ?? {}) as {
                        deploymentContext?: Parameters<ApiClient['checkAwsPermissions']>[0];
                    };
                    const status = await this._apiClient.checkAwsPermissions(p.deploymentContext);
                    this.postMessage('aws.permissions.result', { ok: true, status });
                    this.postMessage('aws.status', status);
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('aws.permissions.result', { ok: false, message: msg });
                }
                break;
            }
            case 'aws.clear': {
                try {
                    await this._coreManager.clearAwsCredentials();
                    await this._coreManager.restart();
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
                    requestId?: number;
                    openFile?: { path: string; content: string };
                    targetFolder?: string;
                    contextFiles?: Array<{ path: string; content: string }>;
                    decisions?: CodeDecisionChoice[];
                };
                await this.handleCodeGenerate(p.instruction ?? '', {
                    requestId: p.requestId,
                    requestWebview,
                    openFile: p.openFile,
                    targetFolder: p.targetFolder ?? '',
                    contextFiles: p.contextFiles ?? [],
                    decisions: p.decisions ?? [],
                });
                break;
            }
            case 'code.plan': {
                // AI-DLC 1단계: 바로 generate 로 가지 않고 /api/code/plan 으로
                // 결정 목록을 먼저 받은 뒤 Webview 모달에서 사용자가 직접 확정한다.
                const p = (payload ?? {}) as {
                    instruction?: string;
                    requestId?: number;
                    openFile?: { path: string; content: string };
                    targetFolder?: string;
                    contextFiles?: Array<{ path: string; content: string }>;
                };
                await this.handleCodePlan(p.instruction ?? '', {
                    requestId: p.requestId,
                    requestWebview,
                    openFile: p.openFile,
                    targetFolder: p.targetFolder ?? '',
                    contextFiles: p.contextFiles ?? [],
                });
                break;
            }
            case 'chat.send': {
                const p = (payload ?? {}) as {
                    id?: string;
                    message?: string;
                    history?: Array<{ role: 'user' | 'assistant'; content: string }>;
                };
                await this.handleChat(p.id ?? '', p.message ?? '', p.history ?? []);
                break;
            }
            case 'code.apply': {
                const { file, content, targetFolder, ackKey } = (payload ?? {}) as { file?: string; content?: string; targetFolder?: string; ackKey?: string };
                await this.handleCodeApply(file ?? '', content ?? '', targetFolder ?? '', ackKey ?? '');
                break;
            }
            case 'code.applyAll': {
                const { ops, targetFolder } = (payload ?? {}) as { ops?: Array<{ file: string; content: string; ackKey?: string }>; targetFolder?: string };
                let okCount = 0;
                for (const op of ops ?? []) {
                    // 파일마다 ackKey 를 되돌려 준다 — 웹뷰가 파일 단위로
                    // 성공/실패를 구분해야 실패한 파일만 재시도할 수 있다.
                    const ok = await this.handleCodeApply(op.file, op.content, targetFolder ?? '', op.ackKey ?? '');
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
            case 'map.project': {
                try {
                    const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
                    if (!root) { this.postMessage('map.error', { message: '분석할 폴더가 열려있지 않습니다.' }); break; }
                    // 서버 불필요 — 확장 내부에서 정적 분석.
                    const data = analyzeProject(root);
                    this.postMessage('map.projectResult', data);
                } catch (err) {
                    this.postMessage('map.error', { message: String(err) });
                }
                break;
            }
            case 'map.file': {
                const { id } = (payload ?? {}) as { id?: string };
                try {
                    const root = vscode.workspace.workspaceFolders?.[0]?.uri;
                    if (!root) { this.postMessage('map.error', { message: '워크스페이스가 열려있지 않습니다.' }); break; }
                    const safe = (id ?? '').replace(/\\/g, '/').replace(/^\/+/, '').split('/').filter((seg) => seg && seg !== '..').join('/');
                    if (!safe) { this.postMessage('map.error', { message: '대상 파일이 없습니다.' }); break; }
                    const abs = vscode.Uri.joinPath(root, safe).fsPath;
                    const data = analyzeFile(abs);
                    this.postMessage('map.fileResult', data);
                } catch (err) {
                    this.postMessage('map.error', { message: String(err) });
                }
                break;
            }
            case 'map.openFile': {
                const { id } = (payload ?? {}) as { id?: string };
                try {
                    const root = vscode.workspace.workspaceFolders?.[0]?.uri;
                    if (!root || !id) { break; }
                    const safe = id.replace(/\\/g, '/').replace(/^\/+/, '').split('/').filter((seg) => seg && seg !== '..').join('/');
                    await vscode.window.showTextDocument(vscode.Uri.joinPath(root, safe));
                } catch (err) {
                    this.postMessage('map.error', { message: String(err) });
                }
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

    /** Core가 제안한 ADR 등 승인된 산출물을 워크스페이스에 안전하게 기록한다. */
    private async writeWorkspaceFile(file: string, content: string): Promise<string> {
        const root = vscode.workspace.workspaceFolders?.[0]?.uri;
        if (!root) { throw new Error('워크스페이스가 열려있지 않습니다.'); }
        const safe = (file || '').replace(/\\/g, '/').replace(/^\/+/, '')
            .split('/').filter((segment) => segment && segment !== '..').join('/');
        if (!safe) { throw new Error('기록할 파일 경로가 올바르지 않습니다.'); }
        const parts = safe.split('/');
        const fileUri = vscode.Uri.joinPath(root, ...parts);
        const parentUri = vscode.Uri.joinPath(root, ...parts.slice(0, -1));
        await vscode.workspace.fs.createDirectory(parentUri);
        await vscode.workspace.fs.writeFile(fileUri, new TextEncoder().encode(content));
        return safe;
    }

    /** 코드 생성 에이전트 — 자연어 요청을 Core 로 보내 ops 를 받아 webview 로 회신. */
    private async handleCodeGenerate(
        instruction: string,
        opts: {
            requestId?: number;
            requestWebview?: vscode.Webview;
            openFile?: { path: string; content: string };
            targetFolder?: string;
            contextFiles?: Array<{ path: string; content: string }>;
            decisions?: CodeDecisionChoice[];
        } = {},
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
                decisions: opts.decisions ?? [],
            });
            // 다음 턴 컨텍스트로 보관
            this._lastCodeOps = (result.ops ?? []).map((op) => ({ path: op.file, content: op.content }));
            this.postMessageToWebview(opts.requestWebview, 'code.result', {
                ...result,
                requestId: opts.requestId,
            });
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            this.postMessageToWebview(opts.requestWebview, 'code.error', {
                requestId: opts.requestId,
                message: msg,
            });
        }
    }

    /** 오른쪽 대화 패널 — 대화만 수행하며 파일 쓰기/코드 적용은 절대 하지 않는다. */
    private async handleChat(
        id: string,
        message: string,
        history: Array<{ role: 'user' | 'assistant'; content: string }>,
    ): Promise<void> {
        if (!message.trim()) { return; }
        const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        try {
            const result = await this._apiClient.chat(message, history, workspacePath);
            this.postMessage('chat.response', { id, reply: result.reply, model: result.model });
        } catch (err) {
            const error = err instanceof Error ? err.message : String(err);
            this.postMessage('chat.error', { id, message: error });
        }
    }

    /** AI-DLC 결정 목록을 Webview 모달로 보내고, 선택·생성은 사용자가 직접 확정한다. */
    private async handleCodePlan(
        instruction: string,
        opts: {
            requestId?: number;
            requestWebview?: vscode.Webview;
            openFile?: { path: string; content: string };
            targetFolder?: string;
            contextFiles?: Array<{ path: string; content: string }>;
        } = {},
    ): Promise<void> {
        if (!instruction.trim()) { return; }
        const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        let attach = opts.openFile;
        if (!attach) {
            const ed = vscode.window.activeTextEditor;
            if (ed && !ed.document.isUntitled) {
                attach = { path: vscode.workspace.asRelativePath(ed.document.uri), content: ed.document.getText() };
            }
        }

        try {
            const plan = await this._apiClient.planCode(instruction, {
                workspacePath,
                openFile: attach,
                contextFiles: opts.contextFiles ?? [],
                targetFolder: opts.targetFolder ?? '',
            });
            this.postMessageToWebview(opts.requestWebview, 'code.planResult', {
                requestId: opts.requestId,
                instruction,
                decisions: plan.decisions ?? [],
            });
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            this.postMessageToWebview(opts.requestWebview, 'code.error', {
                requestId: opts.requestId,
                message: `설계 결정 생성 실패: ${msg}`,
            });
        }
    }

    /** 단일 파일 op 적용 — 워크스페이스에 직접 쓰고 에디터로 연다(Codex 식). 반환: 성공 여부.
     *
     * `ackKey` 는 웹뷰가 붙인 파일 식별자다. 성공(code.applied)·실패(code.error)
     * 어느 쪽이든 그대로 되돌려 준다 — 웹뷰는 이 확인을 받아야만 "적용됨"을
     * 표시한다. 확인 없이 표시하면 쓰기 실패가 성공으로 굳는다.
     */
    private async handleCodeApply(file: string, content: string, targetFolder: string = '', ackKey: string = ''): Promise<boolean> {
        const ack = ackKey ? { ackKey } : {};
        const root = vscode.workspace.workspaceFolders?.[0]?.uri;
        if (!root) {
            this.postMessage('code.error', { message: '워크스페이스가 열려있지 않습니다.', ...ack });
            return false;
        }
        const combined = this._joinFolder(targetFolder, file);
        const safe = combined.replace(/\\/g, '/').replace(/^\/+/, '').split('/').filter((seg) => seg && seg !== '..').join('/');
        if (!safe) {
            this.postMessage('code.error', { message: `잘못된 파일명: ${file}`, ...ack });
            return false;
        }
        const uri = vscode.Uri.joinPath(root, safe);
        try {
            await vscode.workspace.fs.createDirectory(vscode.Uri.joinPath(uri, '..'));
            await vscode.workspace.fs.writeFile(uri, new TextEncoder().encode(content));
            await vscode.window.showTextDocument(uri, { preview: false });
            this.postMessage('code.applied', { file: safe, ok: true, ...ack });
            return true;
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            this.postMessage('code.error', { message: `${safe} 적용 실패: ${msg}`, ...ack });
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

    getWorkspacePanelHtml(webview: vscode.Webview): string {
        return this._getHtmlForWebview(webview, 'workspace');
    }

    private _getHtmlForWebview(webview: vscode.Webview, layout: 'sidebar' | 'workspace' = 'sidebar'): string {
        const nonce = this.getNonce();
        const scriptUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'out', 'webview', 'webview.js')
        );
        const botAvatarUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'media', 'recoder-bot.png')
        );
        const cspConnect = Array.from({ length: 17 }, (_, i) => `http://127.0.0.1:${17894 + i}`).join(' ');

        return `<!DOCTYPE html>
<html lang="ko" data-recoder-layout="${layout}" data-recoder-bot-avatar="${botAvatarUri}">
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
