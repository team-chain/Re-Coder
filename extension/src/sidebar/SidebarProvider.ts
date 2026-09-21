import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { execFileSync } from 'child_process';
import {
    SidebarState,
    Mode,
    AnalyzeRequest,
    PatchProposal,
    InfraFileProposal,
    ResponseProposal,
    CoreHealth,
    DeployMethod,
    AwsStatus,
} from '../types';
import { ApiClient, CodeDecisionChoice } from '../core/ApiClient';
import { CoreManager } from '../core/CoreManager';
import { isCoreConnectionFailure } from '../core/coreReuse';
import {
    CollectedStaticFiles,
    collectStaticFiles,
    describeExcludedFiles,
    pickStaticDir,
    s3ProjectIdentifier,
    STATIC_DIR_CANDIDATES,
} from '../deploy/staticSite';
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

/**
 * S3 재배포용으로 저장소의 이동·재클론에도 변하지 않는 ID를 찾는다.
 *
 * Git 원격 주소만 쓰면 모노레포의 여러 앱이 같은 버킷을 공유한다. 그래서
 * 원격 주소와 **저장소 루트 기준 워크스페이스 경로**를 함께 쓴다. 이 조합은
 * 다른 위치로 재클론해도 유지되면서 `apps/site-a`와 `apps/site-b`를 구분한다.
 */
function s3RepositoryIdentity(workspacePath: string): string {
    try {
        const remote = execFileSync(
            'git',
            ['-C', workspacePath, 'remote', 'get-url', 'origin'],
            { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
        ).trim();
        if (remote) {
            const repositoryRoot = execFileSync(
                'git',
                ['-C', workspacePath, 'rev-parse', '--show-toplevel'],
                { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
            ).trim();
            const realRoot = fs.realpathSync(repositoryRoot);
            const realWorkspace = fs.realpathSync(workspacePath);
            const relative = path.relative(realRoot, realWorkspace);
            const outsideRepository = relative === '..'
                || relative.startsWith(`..${path.sep}`)
                || path.isAbsolute(relative);
            if (!outsideRepository && relative) {
                return `${remote}#${relative.replace(/\\/g, '/')}`;
            }
            return remote;
        }
    } catch {
        // Git 원격이 없는 정적 폴더도 S3 배포는 가능해야 한다.
    }
    try {
        return `local:${fs.realpathSync(workspacePath)}`;
    } catch {
        return `local:${workspacePath}`;
    }
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
        //: 확장 호스트 재시작을 살아남는 저장소(context.globalState). 채팅 승인이
        //: 빈 창에 폴더를 추가하면 VSCode 가 확장을 통째로 재시작하는데, 그때
        //: 아직 발송 못 한 chat.actionAccepted 를 여기 적어 두고 이어서 보낸다.
        private readonly _globalState?: vscode.Memento,
    ) {
        this._state = { currentMode: Mode.BUILD, proposals: [], isLoading: false };
    }

    /** 재시작을 넘겨야 하는 채팅 승인 요청의 globalState 키. */
    private static readonly PENDING_CHAT_ACTION_KEY = 'recoder.pendingChatAction';

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
                //: 재사용한 코어에는 보안 금고의 AWS 연결이 들어가 있지 않다 — 먼저 넣고 진단.
                try { await this.healAwsConnection('startup'); } catch { /* 진단이 알려 준다 */ }
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

    // ── 자가 조치(Self-healing) 레이어 ──────────────────────────────────────
    //
    // "켜져 있어야 동작하는 것" 을 검사 → 자동 조치 → 재검사 → 보고 로 통일한다
    // (보드 카드 「자가 조치(Self-healing) 레이어」). 조치 등급:
    //   · 자동 실행 — 가역·로컬·무비용: 보안 금고의 AWS 연결 재주입, Docker 시작.
    //     단 **"자동 조치함" 을 항상 화면에 남긴다.**
    //   · 안내만 — 제품 밖(설치·모델 동의 등): 원인 + 다음 행동.
    //
    // 코어 인스턴스당 한 번만 시도한다(_healedFor). 매 진단마다 STS 를 두드리거나
    // 같은 실패를 반복하지 않게.

    private _awsHealedFor = '';

    /**
     * 보안 금고의 AWS 연결을 코어에 다시 적용한다.
     *
     * 왜 필요한가: 코어를 새로 spawn 할 때만 env 로 자격증명이 들어간다. 다른
     * 창이 띄운 코어를 **재사용**하거나 사용자가 직접 실행한 코어에 붙으면 아무것도
     * 주입되지 않아 "재시작하면 연결 풀림" 이 됐다(2026-09-20 실기기 NotFound 삽질).
     *
     * 반환: healed(적용함) · not_needed(이미 연결됨) · nothing_stored · failed
     */
    async healAwsConnection(reason: 'startup' | 'diagnostics' | 'fix'): Promise<'healed' | 'not_needed' | 'nothing_stored' | 'failed'> {
        const stored = await this._coreManager.getStoredAwsConnection();
        if (!stored) { return 'nothing_stored'; }
        const roleArn = this._coreManager.getAwsRoleArn();
        try {
            const status = await this._apiClient.getAwsStatus();
            //: 역할 모드로 저장돼 있으면 "역할로" 연결돼 있어야 not_needed 다.
            if (status.ready && (!roleArn || status.storage === 'assumed_role')) { return 'not_needed'; }
        } catch { /* 상태 조회 실패 → 재주입 시도 */ }
        try {
            let status = stored.kind === 'keys'
                ? await this._apiClient.connectAws({
                    accessKeyId: stored.accessKeyId, secretAccessKey: stored.secretAccessKey,
                    region: stored.region, sessionToken: stored.sessionToken,
                })
                : await this._apiClient.connectAwsProfile({ profile: stored.profile, region: stored.region });
            let detail = stored.kind === 'keys' ? '보안 금고의 키' : `프로필 ${stored.profile}`;
            if (roleArn) {
                //: 역할 모드였으면 역할까지 다시 빌린다. 실패(폴백)해도 기반 연결은 살아 있다.
                const role = await this._apiClient.setupAwsRole({
                    profile: stored.kind === 'profile' ? stored.profile : '', region: stored.region,
                });
                if (role.ok && role.status) { status = role.status; detail += ' + 배포 전용 역할'; }
            }
            this.postMessage('aws.status', status);
            this.postMessage('selfHeal', {
                key: 'aws_deploy_ready', action: 'aws_reinject', reason,
                message: `자동 조치함 · ${detail}를 코어에 다시 적용했습니다.`,
            });
            return 'healed';
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            this.postMessage('selfHeal', {
                key: 'aws_deploy_ready', action: 'aws_reinject', reason, failed: true,
                message: `자동 조치 실패 · 보관된 AWS 연결을 다시 적용하지 못했습니다: ${msg}`,
            });
            return 'failed';
        }
    }

    /** 꺼진 Docker 를 코어가 띄우게 한다. 결과를 화면에 "자동 조치함" 으로 남긴다. */
    async healDocker(reason: 'diagnostics' | 'fix'): Promise<boolean> {
        try {
            const r = await this._apiClient.ensureDocker();
            this.postMessage('selfHeal', {
                key: 'docker_ready', action: 'docker_start', reason, failed: !r.ready,
                message: r.ready
                    ? (r.attempted ? `자동 조치함 · Docker Desktop 을 시작했습니다 (${r.waited_seconds}초 대기).` : 'Docker 데몬이 이미 실행 중입니다.')
                    : `자동 조치 실패 · ${r.message}`,
            });
            return r.ready;
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            this.postMessage('selfHeal', { key: 'docker_ready', action: 'docker_start', reason, failed: true, message: `자동 조치 실패 · ${msg}` });
            return false;
        }
    }

    /**
     * 진단 결과를 보고 자동 조치 등급의 항목을 고친다. 고친 게 있으면 true —
     * 호출자는 진단을 한 번 더 돌려 "재검사" 결과를 보여 준다.
     */
    private async _selfHealFromDiagnostics(d: Record<string, unknown>): Promise<boolean> {
        const key = this._coreManager.coreInstanceKey();
        if (this._awsHealedFor === key) { return false; }
        this._awsHealedFor = key;
        let healed = false;
        const notReady = (k: string) => d[k] !== undefined && d[k] !== 'ready';
        if (notReady('aws_deploy_ready') || notReady('ai_ready')) {
            healed = (await this.healAwsConnection('diagnostics')) === 'healed' || healed;
        }
        if (notReady('docker_ready')) {
            healed = (await this.healDocker('diagnostics')) || healed;
        }
        return healed;
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
                    case 'ai_ready': {  // Bedrock 도 AWS 자격증명을 사용
                        //: 자동 조치 먼저 — 보안 금고에 연결이 있으면 다시 넣는다.
                        //: 없거나 실패했을 때만 사용자에게 입력을 받는다.
                        const outcome = await this.healAwsConnection('fix');
                        if (outcome === 'healed' || outcome === 'not_needed') {
                            void this.handleMessage({ type: 'runDiagnostics', payload: {} });
                            break;
                        }
                        await vscode.commands.executeCommand('recoder.awsConfigure');
                        break;
                    }
                    case 'github_ready':
                        await vscode.commands.executeCommand('recoder.githubLogin');
                        break;
                    case 'docker_ready': {
                        //: 자동 조치 — 코어가 Docker Desktop 을 직접 띄운다(가역·로컬·무비용).
                        //: 그래도 안 되면(미설치 등) 안내 + 다운로드 링크.
                        const ready = await this.healDocker('fix');
                        if (!ready) {
                            const selected = await vscode.window.showInformationMessage(
                                'Docker 를 자동으로 시작하지 못했습니다. Docker Desktop 이 설치돼 있는지 확인해 주세요.',
                                'Docker Desktop 다운로드',
                            );
                            if (selected === 'Docker Desktop 다운로드') {
                                void vscode.env.openExternal(
                                    vscode.Uri.parse('https://www.docker.com/products/docker-desktop'),
                                );
                            }
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
            case 'generateCompose': {
                const p = (payload ?? {}) as { workspacePath?: string; projectId?: string };
                await this.handleGenerateCompose(
                    p.workspacePath || (vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? ''),
                    p.projectId,
                );
                break;
            }
            case 'approveDockerfile': {
                const { proposalId, approved } = payload as { proposalId: string; approved: boolean };
                const dfResult = await this._apiClient.approveDockerfile(proposalId, approved);
                this.postMessage('stateUpdate', { ...this._state });
                if (dfResult.status === 'error') {
                    this.postMessage('errorMessage', { message: '인프라 파일 승인 실패' });
                } else {
                    // 저장이 끝난 뒤에만 웹뷰가 다음 단계(Trivy 또는 완료)로
                    // 넘어간다. 고정 지연으로 추측하면 느린 디스크에서 저장보다
                    // 스캔이 먼저 실행될 수 있다.
                    this.postMessage('infraApprovalResult', {
                        ...dfResult,
                        proposal_id: proposalId,
                        approved,
                    });
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
                    //: 요청 자체가 실패해도 화면에는 "Error: trivy 스캔 실패" 같은 raw
                    //: 문자열이 아니라 **미검증 + 원인 + 다음 행동** 이 떠야 한다
                    //: (보드 카드 「스캔 실패 표시가 raw 에러」). 코어가 분류한 실패와
                    //: 같은 모양으로 내려보내 화면이 한 경로로 그린다.
                    const raw = err instanceof Error ? err.message : String(err);
                    this.postMessage('scanResult', {
                        status: 'error',
                        scan_type: scanType,
                        target: targetPath ?? scanWs,
                        critical_count: 0, high_count: 0, medium_count: 0, findings: [],
                        reason_code: 'request_failed',
                        cause: '코어와의 스캔 요청이 끝나기 전에 끊겼습니다.',
                        next_action: '코어 상태를 확인하고 다시 검사하세요. 반복되면 코어를 재시작하세요.',
                        summary: '확인하지 못했습니다 — 코어와의 스캔 요청이 끝나기 전에 끊겼습니다.',
                        message: raw,
                    });
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
                    // 「다시 검사」는 **재연결 시도까지 포함해야** 한다.
                    //
                    // 예전에는 실패를 그대로 화면에 던졌다. 코어 연결이 끊긴
                    // 상태에서는 몇 번을 눌러도 같은 `fetch failed` 만 반복됐고,
                    // 사용자가 할 수 있는 건 창 리로드뿐이었다. 여기서 한 번
                    // 재연결한 뒤 다시 시도한다.
                    const message = err instanceof Error ? err.message : String(err);

                    // **연결 문제일 때만 재연결한다.**
                    //
                    // 예전에는 모든 실패를 연결 끊김으로 취급했다. 코어는
                    // 멀쩡한데 preflight 가 500 을 내면 사용자에게는 "코어가
                    // 실행 중인지 확인해 주세요" 가 뜨고 진짜 원인은 사라졌다.
                    // dev 모드에서는 ensureRunning() 이 cleanupStale() 로 이어져
                    // **멀쩡한 코어를 죽이기까지** 했다.
                    if (!isCoreConnectionFailure(message)) {
                        this.postMessageToWebview(
                            requestWebview, 'workspace.deploy.preflightError', { message },
                        );
                        break;
                    }

                    try {
                        await this._coreManager.ensureRunning();
                        await this._coreManager.refreshToken();
                        const retried = await this._apiClient.getDeployPreflight(workspacePath);
                        this.postMessageToWebview(
                            requestWebview, 'workspace.deploy.preflightResult', retried,
                        );
                        break;
                    } catch (retryErr) {
                        // 재시도의 **실제 오류**를 보여 준다. 예전에는 여기서
                        // 통째로 삼키고 고정 문구만 붙였다.
                        const retryMessage = retryErr instanceof Error
                            ? retryErr.message : String(retryErr);
                        this.postMessageToWebview(
                            requestWebview, 'workspace.deploy.preflightError',
                            { message: `${message} (재연결 후에도 실패: ${retryMessage})` },
                        );
                    }
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
                        //: 검사 → 자동 조치 → 재검사. 고친 게 있으면 한 번만 다시 돈다
                        //: (인스턴스당 1회 가드가 _selfHealFromDiagnostics 안에 있다).
                        const healed = await this._selfHealFromDiagnostics(normalized as unknown as Record<string, unknown>);
                        if (healed) {
                            this.postMessage('stateUpdate', this._state);
                            void this.handleMessage({ type: 'runDiagnostics', payload: {} });
                            break;
                        }
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
                //: 채팅 승인 인계 — 승인 카드가 빈 창에 폴더를 추가하면 확장이
                //: 재시작돼 chat.actionAccepted 가 유실된다(위 handleChatApproveAction
                //: 주석 참고). 재시작 전에 globalState 에 적어 둔 요청이 있으면
                //: **먼저 ready 를 보낸 웹뷰 하나가** 이어받아 발송한다. 키를 먼저
                //: 지워서 사이드바·큰 화면이 둘 다 ready 를 보내도 한 번만 나간다.
                const pending = this._globalState?.get<{
                    instruction: string; targetFolder: string; absolutePath: string;
                    folderCreated: boolean; ts: number;
                }>(SidebarProvider.PENDING_CHAT_ACTION_KEY);
                if (pending) {
                    void this._globalState?.update(SidebarProvider.PENDING_CHAT_ACTION_KEY, undefined);
                    //: 오래된 메모(10분 초과)는 버린다 — 사용자가 이미 다른 일을
                    //: 시작한 창에 옛 요청이 불쑥 끼어드는 것을 막는다.
                    if (Date.now() - pending.ts <= 10 * 60 * 1000 && pending.instruction) {
                        this.postMessageToWebview(requestWebview, 'chat.actionAccepted', {
                            id: `restored-${pending.ts}`,
                            requestId: Date.now(),
                            instruction: pending.instruction,
                            targetFolder: pending.targetFolder,
                            absolutePath: pending.absolutePath,
                            folderCreated: pending.folderCreated,
                            addedToWorkspace: true,
                            restoredAfterReload: true,
                        });
                    }
                }
                break;
            }
            // ── AWS Credentials / Status (§S-2 — /api/aws/* 라우트) ────────
            case 'aws.onboarding': {
                //: 원클릭 IAM 셋업 — 보드 카드 「AWS 온보딩 마찰 제거」.
                //: 템플릿이 호스팅돼 있으면 quick-create(클릭 한 번), 아니면
                //: 템플릿을 클립보드에 복사하고 콘솔 업로드 화면을 연다.
                try {
                    const link = await this._apiClient.getAwsOnboardingLink();
                    if (!link.template_hosted) {
                        await vscode.env.clipboard.writeText(link.template_body);
                    }
                    const url = link.quick_create_url || link.console_upload_url;
                    await vscode.env.openExternal(vscode.Uri.parse(url));
                    this.postMessage('aws.onboarding.result', {
                        hosted: link.template_hosted,
                        steps: link.steps,
                        stack_name: link.stack_name,
                        action_count: link.action_count,
                    });
                } catch (err) {
                    this.postMessage('aws.onboarding.result', { error: String(err) });
                }
                break;
            }
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
            case 'workspace.deploy.s3': {
                // **코어에는 이 라우트가 완성돼 있었고 확장이 부르지 않았다.**
                // 버킷 생성·공개 설정·업로드·URL 조립까지 다 되어 있는데,
                // 사용자 파일을 읽어 보내는 쪽이 없어서 통째로 도달 불가능이었다.
                const p = (payload ?? {}) as { dir?: string; region?: string };
                const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
                if (!workspacePath) {
                    this.postMessage('workspace.deploy.s3.result', {
                        ok: false, message: '열린 워크스페이스가 없습니다.',
                    });
                    break;
                }

                const dirLabel = (p.dir ?? '').trim();
                const root = dirLabel ? path.join(workspacePath, dirLabel) : workspacePath;
                if (!fs.existsSync(root)) {
                    this.postMessage('workspace.deploy.s3.result', {
                        ok: false,
                        message: `폴더를 찾을 수 없습니다: ${dirLabel || '.'} — 빌드를 먼저 실행했는지 확인하세요.`,
                    });
                    break;
                }

                let realRoot: string;
                try {
                    // readdirSync는 루트 자체(예: dist)가 심볼릭 링크면 그 링크를
                    // 먼저 따라간다. 자식 Dirent 검사만으로는 워크스페이스 밖의
                    // 파일이 공개되는 것을 막을 수 없으므로, 수집 전에 실경로를
                    // 비교한다.
                    const realWorkspacePath = fs.realpathSync(workspacePath);
                    realRoot = fs.realpathSync(root);
                    const relativeRoot = path.relative(realWorkspacePath, realRoot);
                    const outsideWorkspace = relativeRoot === '..'
                        || relativeRoot.startsWith(`..${path.sep}`)
                        || path.isAbsolute(relativeRoot);
                    if (outsideWorkspace) {
                        this.postMessage('workspace.deploy.s3.result', {
                            ok: false,
                            message: `선택한 폴더가 워크스페이스 밖을 가리킵니다: ${dirLabel || '.'}. 실제 배포 산출물 폴더를 워크스페이스 안에 복사한 뒤 다시 시도하세요.`,
                        });
                        break;
                    }
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('workspace.deploy.s3.result', {
                        ok: false,
                        message: `선택한 폴더의 실제 경로를 확인하지 못했습니다: ${msg}`,
                    });
                    break;
                }

                let collected: CollectedStaticFiles;
                try {
                    collected = collectStaticFiles(realRoot, fs, path.join, dirLabel);
                } catch (err) {
                    // 상한 초과는 자르지 않고 알린다. 조용히 30개만 올리면
                    // 사이트가 반쯤 올라간 채로 "배포 성공" 이 된다.
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('workspace.deploy.s3.result', { ok: false, message: msg });
                    break;
                }
                const files = collected.files;

                if (!files.length) {
                    //: 전부 필터에 걸러진 경우, "파일이 없습니다"만 보여주면
                    //: 사용자는 눈앞의 파일들이 왜 안 올라갔는지 알 수 없다.
                    //: 무엇이 왜 제외됐는지를 같은 메시지에 담는다.
                    const why = describeExcludedFiles(collected);
                    this.postMessage('workspace.deploy.s3.result', {
                        ok: false,
                        message: `${dirLabel || '워크스페이스 루트'} 에 올릴 정적 파일이 없습니다. ` +
                            `빌드 산출물 폴더(dist·build 등)를 지정했는지 확인하세요.${why ? ` ${why}` : ''}`,
                    });
                    break;
                }

                try {
                    //: 진행 상황을 흘려받아 웹뷰로 중계한다. 예전에는 요청
                    //: 하나에 응답 하나라, 파일 수십 개를 올리는 동안 화면이
                    //: "배포 중…" 에서 멈춰 있었고 실패해도 어느 단계인지
                    //: 알 수 없었다(보드 이슈).
                    const result = await this._apiClient.deployS3Stream({
                        // 로컬 폴더 경로가 아닌 Git 원격 주소를 지문으로 쓴다.
                        // 같은 저장소를 다른 위치에서 열어도 기존 공개 버킷을
                        // 갱신하고, 이전 배포 버킷을 고아 상태로 남기지 않는다.
                        project: s3ProjectIdentifier(
                            s3RepositoryIdentity(workspacePath),
                            path.basename(workspacePath),
                        ),
                        files,
                        region: (p.region ?? '').trim() || undefined,
                    }, (event) => {
                        this.postMessage('workspace.deploy.s3.progress', event);
                    });
                    this.postMessage('workspace.deploy.s3.result', {
                        ok: true,
                        result: {
                            ...result,
                            //: 필터가 무엇을 걸렀는지 결과 화면까지 전달한다.
                            //: 조용히 빼면 이 필터는 없는 것과 같다.
                            excluded_sensitive: collected.excludedSensitive,
                            excluded_non_asset: collected.excludedNonAsset,
                            excluded_note: describeExcludedFiles(collected) || undefined,
                        },
                    });
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('workspace.deploy.s3.result', { ok: false, message: msg });
                }
                break;
            }
            case 'workspace.deploy.s3.dirs': {
                // 어떤 폴더가 있는지 웹뷰가 알 방법이 없다. 후보를 골라 준다.
                const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
                let dirs: string[] = [];
                if (workspacePath) {
                    try {
                        dirs = fs.readdirSync(workspacePath, { withFileTypes: true })
                            .filter(e => e.isDirectory())
                            .map(e => e.name);
                    } catch { dirs = []; }
                }
                this.postMessage('workspace.deploy.s3.dirs', {
                    suggested: pickStaticDir(dirs),
                    candidates: STATIC_DIR_CANDIDATES.filter(c => dirs.includes(c)),
                });
                break;
            }
            case 'aws.policy': {
                // **코어에는 이 엔드포인트가 오래전부터 있었고, 확장이 한 번도
                // 부르지 않았다.** 그래서 사용자는 "권한이 없습니다" 라는 배포
                // 실패만 보고 무엇을 허용해야 하는지는 알 수 없었다.
                const p = (payload ?? {}) as {
                    targets?: string[];
                    cluster?: string;
                    service?: string;
                    ecrRepo?: string;
                    region?: string;
                };
                try {
                    const policy = await this._apiClient.getAwsPolicy(p);
                    if (!policy) {
                        this.postMessage('aws.policy.result', {
                            ok: false,
                            message: '권한표를 가져오지 못했습니다. 코어가 실행 중인지 확인해 주세요.',
                        });
                        break;
                    }
                    this.postMessage('aws.policy.result', { ok: true, policy });
                } catch (err) {
                    // 여기서 실패해도 배포가 막히는 건 아니다 — 도움을 주려던
                    // 경로이므로 원인만 그대로 전한다.
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('aws.policy.result', { ok: false, message: msg });
                }
                break;
            }
            case 'aws.policy.copy': {
                // 클립보드 복사는 웹뷰가 아니라 **확장에서** 한다. 웹뷰의
                // navigator.clipboard 는 VS Code 웹뷰 샌드박스에서 막히는
                // 경우가 있어, 조용히 아무 일도 안 일어난 것처럼 보인다.
                const text = ((payload ?? {}) as { text?: string }).text ?? '';
                if (!text.trim()) {
                    this.postMessage('aws.policy.copied', { ok: false });
                    break;
                }
                try {
                    await vscode.env.clipboard.writeText(text);
                    this.postMessage('aws.policy.copied', { ok: true });
                } catch {
                    // onDidReceiveMessage는 이 async handler를 기다리지 않는다.
                    // 실패를 회신하지 않으면 웹뷰 버튼이 아무 반응 없는 것처럼 보인다.
                    this.postMessage('aws.policy.copied', { ok: false });
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
            case 'aws.connect.profile': {
                //: 키 입력 없이 ~/.aws 프로필로 연결한다. 비밀 값은 이 확장을
                //: 한 번도 거치지 않는다 — 코어의 boto3 가 파일에서 직접 읽는다.
                const p = (payload ?? {}) as { profile?: string; region?: string };
                const profile = (p.profile ?? '').trim();
                if (!profile) {
                    this.postMessage('aws.configure.result', { ok: false, message: '프로필 이름이 비어있습니다.' });
                    break;
                }
                try {
                    const status = await this._apiClient.connectAwsProfile({
                        profile,
                        region: (p.region ?? '').trim(),
                    });
                    //: 코어 재시작 후에도 이 선택이 살아남도록 이름만 보관한다.
                    await this._coreManager.storeAwsProfile(profile, status.region ?? '');
                    //: 키 연결과 같은 결과 채널을 쓴다 — 화면은 "어떻게 연결됐나"
                    //: 가 아니라 "연결됐나"에 반응하면 된다.
                    this.postMessage('aws.configure.result', { ok: true, status });
                    this.postMessage('aws.status', status);
                    void this.handleMessage({ type: 'runDiagnostics', payload: {} });
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('aws.configure.result', { ok: false, message: msg });
                    this.postMessage('errorMessage', { message: `AWS 프로필 연결 실패: ${msg}` });
                }
                break;
            }
            case 'aws.role.setup': {
                //: 프로그램 안에서 배포 전용 최소권한 역할을 만들고 빌린다 —
                //: 보드 카드 「AWS 온보딩 마찰 제거」의 콘솔 없는 경로.
                //: profile 이 있으면 그 프로필이 기반, 없으면 지금 연결된 자격증명.
                //: 권한이 모자라면(ok=false) 프로필로는 그냥 연결해 두고 화면에
                //: 콘솔 폴백을 안내한다 — 배포는 되되 권한만 안 좁혀진 상태다.
                const p = (payload ?? {}) as { profile?: string; region?: string };
                const profile = (p.profile ?? '').trim();
                const region = (p.region ?? '').trim();
                try {
                    const result = await this._apiClient.setupAwsRole({ profile, region });
                    if (result.ok && result.status) {
                        if (profile) {
                            await this._coreManager.storeAwsProfile(profile, result.status.region ?? '');
                        }
                        //: 저장하는 건 역할 ARN 하나 — 비밀이 아니다. 임시 자격증명은
                        //: 코어가 재시작마다 기반으로 다시 빌린다.
                        await this._coreManager.storeAwsRole(result.role?.role_arn ?? result.status.role_arn ?? '');
                        this.postMessage('aws.role.result', {
                            ok: true, mode: 'role', message: result.message, role: result.role, status: result.status,
                        });
                        this.postMessage('aws.configure.result', { ok: true, status: result.status });
                        this.postMessage('aws.status', result.status);
                        void this.handleMessage({ type: 'runDiagnostics', payload: {} });
                        break;
                    }
                    //: 콘솔 폴백. 프로필이 주어졌으면 최소한 프로필 연결은 해 준다.
                    let status: AwsStatus | undefined;
                    if (profile) {
                        status = await this._apiClient.connectAwsProfile({ profile, region });
                        await this._coreManager.storeAwsProfile(profile, status.region ?? '');
                        this.postMessage('aws.configure.result', { ok: true, status });
                        this.postMessage('aws.status', status);
                        void this.handleMessage({ type: 'runDiagnostics', payload: {} });
                    }
                    this.postMessage('aws.role.result', {
                        ok: false, mode: result.mode, message: result.message,
                        denied_action: result.denied_action, status,
                    });
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('aws.role.result', { ok: false, mode: 'error', message: msg });
                    this.postMessage('aws.configure.result', { ok: false, message: msg });
                    this.postMessage('errorMessage', { message: `AWS 역할 설정 실패: ${msg}` });
                }
                break;
            }
            case 'aws.role.refresh': {
                try {
                    const status = await this._apiClient.refreshAwsRole();
                    this.postMessage('aws.role.result', { ok: true, mode: 'refreshed', message: status.message, status });
                    this.postMessage('aws.status', status);
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    this.postMessage('aws.role.result', { ok: false, mode: 'error', message: msg });
                }
                break;
            }
            case 'aws.clear': {
                try {
                    await this._coreManager.clearAwsCredentials();
                    //: 코어 프로세스 안의 자격증명도 지운다. 재시작만으로는 부족하다 —
                    //: 확장이 **남이 띄운 코어를 재사용** 중이면(터미널에서 python main.py,
                    //: 다른 창이 띄운 코어) restart 는 그 코어를 죽이지 않으므로 env 의
                    //: 키가 그대로 남아 "연결 해제" 를 눌러도 다시 연결됨으로 뜬다
                    //: (2026-09-21 실기기). 코어 API 로 지우면 소유와 무관하게 해제된다.
                    try { await this._apiClient.clearAws(); } catch { /* 코어가 죽어 있으면 재시작이 처리 */ }
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
                await this.handleChat(p.id ?? '', p.message ?? '', p.history ?? [], requestWebview);
                break;
            }
            case 'chat.approveAction': {
                //: 승인 카드의 [승인하고 생성]. 여기서만 채팅이 코드 생성으로 넘어간다.
                const p = (payload ?? {}) as { id?: string; instruction?: string; targetFolder?: string };
                await this.handleChatApproveAction(p.id ?? '', p.instruction ?? '', p.targetFolder ?? '', requestWebview);
                break;
            }
            case 'chat.pickActionFolder': {
                //: 승인 카드의 [위치 변경]. 워크스페이스 밖 폴더도 고를 수 있고, 그 경우
                //: 절대경로를 그대로 돌려준다 — 예전 code.pickFolder 처럼 조용히 루트('')로
                //: 바꾸지 않는다.
                const p = (payload ?? {}) as { id?: string };
                const picked = await vscode.window.showOpenDialog({
                    canSelectFolders: true, canSelectFiles: false, canSelectMany: false,
                    openLabel: '이 폴더에 생성',
                    defaultUri: vscode.workspace.workspaceFolders?.[0]?.uri,
                });
                if (picked && picked[0]) {
                    this.postMessageToWebview(requestWebview, 'chat.actionFolderPicked', { id: p.id ?? '', folder: this._describeTargetFolder(picked[0].fsPath).display });
                }
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
                const resolved = this._resolveWriteRoot(targetFolder ?? '');
                if (!resolved) { this.postMessage('code.error', { message: '워크스페이스가 열려있지 않습니다.' }); break; }
                await this.handleCodeDiff(this._joinFolder(resolved.relFolder, file ?? ''), content ?? '', resolved.root);
                break;
            }
            case 'code.pickFolder': {
                const picked = await vscode.window.showOpenDialog({
                    canSelectFolders: true, canSelectFiles: false, canSelectMany: false,
                    openLabel: '이 폴더에 생성',
                    defaultUri: vscode.workspace.workspaceFolders?.[0]?.uri,
                });
                if (picked && picked[0]) {
                    //: 워크스페이스 안이면 상대경로, 밖이면 절대경로. 예전에는 밖을 고르면
                    //: ''(루트) 로 바꿔 버려서, 사용자는 다른 폴더를 골랐는데 파일은 프로젝트
                    //: 루트에 생겼다. 밖의 폴더는 사용자가 다이얼로그에서 직접 고른 것이므로
                    //: 그 자리에서 워크스페이스에 추가한다 — 파일 쓰기는 워크스페이스 안으로만
                    //: 향한다는 규칙(_resolveWriteRoot)을 그대로 두기 위해서다.
                    const desc = this._describeTargetFolder(picked[0].fsPath);
                    if (!desc.insideWorkspace) {
                        const already = (vscode.workspace.workspaceFolders ?? []).some((f) => f.uri.fsPath === desc.absolute);
                        if (!already) {
                            const count = vscode.workspace.workspaceFolders?.length ?? 0;
                            vscode.workspace.updateWorkspaceFolders(count, 0, { uri: vscode.Uri.file(desc.absolute) });
                        }
                    }
                    this.postMessage('code.folderPicked', { folder: desc.display });
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
            case 'adr.list': {
                //: docs/adr/*.md 목록. 제목은 첫 번째 '# ' 줄, 없으면 파일명.
                const root = vscode.workspace.workspaceFolders?.[0]?.uri;
                if (!root) { this.postMessageToWebview(requestWebview, 'adr.listResult', { items: [], error: '워크스페이스가 열려있지 않습니다.' }); break; }
                try {
                    const dir = vscode.Uri.joinPath(root, 'docs', 'adr');
                    let entries: [string, vscode.FileType][] = [];
                    try { entries = await vscode.workspace.fs.readDirectory(dir); } catch { entries = []; }
                    const items: Array<{ file: string; title: string; id: string }> = [];
                    for (const [name, type] of entries) {
                        if (type !== vscode.FileType.File || !name.toLowerCase().endsWith('.md')) { continue; }
                        let title = name.replace(/\.md$/i, '');
                        try {
                            const buf = await vscode.workspace.fs.readFile(vscode.Uri.joinPath(dir, name));
                            const text = new TextDecoder().decode(buf).slice(0, 4000);
                            const m = /^#\s+(.+)$/m.exec(text);
                            if (m) { title = m[1].trim(); }
                        } catch { /* 제목 없이 진행 */ }
                        const idm = /^(ADR-[A-Za-z]?\d+)/i.exec(name);
                        items.push({ file: `docs/adr/${name}`, title, id: idm ? idm[1].toUpperCase() : '' });
                    }
                    //: 최신이 위로 — 번호 내림차순, 번호 없는 것은 뒤로.
                    items.sort((a, b) => (b.id || '').localeCompare(a.id || '', undefined, { numeric: true }));
                    this.postMessageToWebview(requestWebview, 'adr.listResult', { items });
                } catch (err) {
                    this.postMessageToWebview(requestWebview, 'adr.listResult', { items: [], error: String(err) });
                }
                break;
            }
            case 'adr.read': {
                const { file } = (payload ?? {}) as { file?: string };
                const root = vscode.workspace.workspaceFolders?.[0]?.uri;
                const safe = (file ?? '').replace(/\\/g, '/').replace(/^\/+/, '').split('/').filter((seg) => seg && seg !== '..').join('/');
                if (!root || !safe.startsWith('docs/adr/')) { this.postMessageToWebview(requestWebview, 'adr.readResult', { file, error: '읽을 수 없는 경로입니다.' }); break; }
                try {
                    const buf = await vscode.workspace.fs.readFile(vscode.Uri.joinPath(root, safe));
                    this.postMessageToWebview(requestWebview, 'adr.readResult', { file, content: new TextDecoder().decode(buf) });
                } catch (err) {
                    this.postMessageToWebview(requestWebview, 'adr.readResult', { file, error: String(err) });
                }
                break;
            }
            case 'adr.open': {
                const { file } = (payload ?? {}) as { file?: string };
                const root = vscode.workspace.workspaceFolders?.[0]?.uri;
                const safe = (file ?? '').replace(/\\/g, '/').replace(/^\/+/, '').split('/').filter((seg) => seg && seg !== '..').join('/');
                if (root && safe) {
                    try { await vscode.window.showTextDocument(vscode.Uri.joinPath(root, safe), { preview: false }); } catch { /* ignore */ }
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
    private async handleCodeDiff(file: string, content: string, rootOverride?: vscode.Uri): Promise<void> {
        if (!this._codegenProviderRegistered) {
            const docs = this._codegenDocs;
            vscode.workspace.registerTextDocumentContentProvider('recoder-codegen', {
                provideTextDocumentContent(uri: vscode.Uri): string {
                    return docs.get(uri.path.replace(/^\//, '')) ?? '';
                },
            });
            this._codegenProviderRegistered = true;
        }
        const root = rootOverride ?? vscode.workspace.workspaceFolders?.[0]?.uri;
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
        //: 승인 카드로 들어온 워크스페이스 밖 폴더면 Core 에는 그 폴더를 프로젝트 루트로
        //: 알려준다. 예전엔 항상 첫 워크스페이스 폴더를 넘겨서, Re-Coder 저장소를 열어둔 채
        //: 다른 곳에 만들라고 해도 Core 는 Re-Coder 를 스캔하고 거기 기준으로 생성했다.
        const scope = this._codeScope(opts.targetFolder ?? '');
        const workspacePath = scope.workspacePath;
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
                targetFolder: scope.targetFolder,
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
        requestWebview?: vscode.Webview,
    ): Promise<void> {
        if (!message.trim()) { return; }
        const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        try {
            const result = await this._apiClient.chat(message, history, workspacePath);
            let action = result.action ?? null;
            if (action) {
                //: 경로를 사람이 읽을 형태로 정리해 카드에 보여준다. 실제 폴더 생성·워크스페이스
                //: 추가는 승인을 누른 뒤(handleChatApproveAction)에만 한다.
                const desc = this._describeTargetFolder(action.target_folder || '');
                action = { ...action, target_folder: desc.display };
                this.postMessageToWebview(requestWebview, 'chat.response', {
                    id, reply: result.reply, model: result.model, action,
                    target: { display: desc.display, absolute: desc.absolute, insideWorkspace: desc.insideWorkspace, exists: desc.exists, workspaceName: desc.workspaceName },
                });
                return;
            }
            this.postMessageToWebview(requestWebview, 'chat.response', { id, reply: result.reply, model: result.model, action: null });
        } catch (err) {
            const error = err instanceof Error ? err.message : String(err);
            this.postMessageToWebview(requestWebview, 'chat.error', { id, message: error });
        }
    }

    /**
     * 대상 폴더 문자열을 해석한다.
     *
     * - ''            → 워크스페이스 루트
     * - 상대경로       → 워크스페이스 루트 기준
     * - ~/..., 절대경로 → 그대로. 워크스페이스 안이면 상대경로로 바꿔 표시한다.
     *
     * 반환 display 는 웹뷰·Core 에 넘기는 값이다: 안이면 상대경로(''=루트), 밖이면 절대경로.
     */
    private _describeTargetFolder(input: string): { display: string; absolute: string; insideWorkspace: boolean; exists: boolean; workspaceName: string } {
        const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        const workspaceName = root ? path.basename(root) : '';
        let raw = (input || '').trim().replace(/\\/g, '/');
        if (raw.startsWith('~')) {
            raw = path.join(process.env.HOME || process.env.USERPROFILE || '', raw.slice(1));
        }
        let absolute: string;
        if (!raw) {
            absolute = root;
        } else if (path.isAbsolute(raw)) {
            absolute = path.normalize(raw);
        } else {
            absolute = root ? path.normalize(path.join(root, raw)) : path.resolve(raw);
        }
        const rel = root ? path.relative(root, absolute) : '';
        const insideWorkspace = !!root && rel !== '' ? !rel.startsWith('..') && !path.isAbsolute(rel) : !!root && absolute === root;
        const display = insideWorkspace ? rel.replace(/\\/g, '/') : absolute;
        let exists = false;
        try { exists = fs.existsSync(absolute); } catch { /* ignore */ }
        return { display, absolute, insideWorkspace, exists, workspaceName };
    }

    /**
     * 승인 카드 [승인하고 생성].
     *
     * 1) 대상 폴더가 없으면 만든다.
     * 2) 워크스페이스 밖이면 워크스페이스 폴더로 추가한다 — 이후 파일 쓰기는 항상
     *    워크스페이스 안으로만 향한다는 기존 안전장치를 그대로 유지하기 위해서다.
     * 3) 웹뷰에 chat.actionAccepted 를 보낸다. App 이 Build 화면으로 전환하고 CodeAgent 가
     *    이 요청을 자기 턴으로 등록해 code.plan 을 보낸다. 결정 카드 → 생성 → diff → 적용은
     *    전부 기존 흐름이다.
     */
    private async handleChatApproveAction(id: string, instruction: string, targetFolder: string, requestWebview?: vscode.Webview): Promise<void> {
        //: 회신은 **승인을 누른 웹뷰에만** 보낸다. 사이드바와 큰 작업 화면은 같은 React 앱을
        //: 각각 띄우고 있어서, 전체에 뿌리면 두 CodeAgent 가 저마다 code.plan 을 보내
        //: 설계 결정이 두 번 만들어지고 모달이 엉뚱한 창에 뜬다.
        if (!instruction.trim()) {
            this.postMessageToWebview(requestWebview, 'chat.actionError', { id, message: '생성할 내용이 비어 있습니다.' });
            return;
        }
        const desc = this._describeTargetFolder(targetFolder);
        let folderCreated = false;
        let addedToWorkspace = false;
        try {
            if (!desc.exists) {
                await vscode.workspace.fs.createDirectory(vscode.Uri.file(desc.absolute));
                folderCreated = true;
            }
            if (!desc.insideWorkspace) {
                const already = (vscode.workspace.workspaceFolders ?? []).some((f) => f.uri.fsPath === desc.absolute);
                if (!already) {
                    const count = vscode.workspace.workspaceFolders?.length ?? 0;
                    //: **여기서 확장이 통째로 재시작될 수 있다.** 빈 창(폴더 0개)에
                    //: 폴더를 추가하면 VSCode 는 확장 호스트를 재시작하고, 아래의
                    //: chat.actionAccepted 는 영영 발송되지 못한다 — 화면은 복원된
                    //: 스피너만 돌리는 "무한 로딩"이 된다 (실기기에서 실제 발생).
                    //: 그래서 추가 **직전에** 재시작을 살아남는 globalState 에 요청을
                    //: 적어 두고, 재시작 후 첫 webview.ready 가 이어서 발송한다.
                    //: 재시작이 없었으면 아래에서 바로 지운다.
                    await this._globalState?.update(SidebarProvider.PENDING_CHAT_ACTION_KEY, {
                        instruction,
                        targetFolder: desc.display,
                        absolutePath: desc.absolute,
                        folderCreated,
                        ts: Date.now(),
                    });
                    const ok = vscode.workspace.updateWorkspaceFolders(count, 0, { uri: vscode.Uri.file(desc.absolute) });
                    if (!ok) {
                        await this._globalState?.update(SidebarProvider.PENDING_CHAT_ACTION_KEY, undefined);
                        throw new Error('워크스페이스에 폴더를 추가하지 못했습니다.');
                    }
                    addedToWorkspace = true;
                }
            }
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            this.postMessageToWebview(requestWebview, 'chat.actionError', { id, message: `폴더 준비 실패: ${msg}` });
            return;
        }
        //: CodeAgent 의 턴 번호(1,2,3…)와 겹치지 않도록 시각 기반 id 를 쓴다.
        const requestId = Date.now();
        this.postMessageToWebview(requestWebview, 'chat.actionAccepted', {
            id, requestId, instruction,
            targetFolder: desc.display,
            absolutePath: desc.absolute,
            folderCreated, addedToWorkspace,
        });
        //: 재시작 없이 여기까지 왔으면 인계 메모는 필요 없다 — 지워서
        //: 다음 webview.ready 가 같은 요청을 중복 발송하지 않게 한다.
        await this._globalState?.update(SidebarProvider.PENDING_CHAT_ACTION_KEY, undefined);
    }

    /** targetFolder 가 절대경로(워크스페이스 밖)면 그 폴더를, 아니면 첫 워크스페이스 폴더를 루트로 쓴다. */
    private _resolveWriteRoot(targetFolder: string): { root: vscode.Uri; relFolder: string } | null {
        const raw = (targetFolder || '').trim();
        if (raw && (path.isAbsolute(raw) || raw.startsWith('~'))) {
            const desc = this._describeTargetFolder(raw);
            if (desc.insideWorkspace) {
                const root = vscode.workspace.workspaceFolders?.[0]?.uri;
                return root ? { root, relFolder: desc.display } : null;
            }
            //: 밖의 절대경로는 워크스페이스에 추가된 폴더여야만 쓴다. 승인 카드를 거치지 않은
            //: 임의 경로(예: 오래된 웹뷰 상태)로 파일이 새는 것을 막는다.
            const allowed = (vscode.workspace.workspaceFolders ?? []).some((f) => desc.absolute === f.uri.fsPath || desc.absolute.startsWith(f.uri.fsPath + path.sep));
            if (!allowed) { return null; }
            return { root: vscode.Uri.file(desc.absolute), relFolder: '' };
        }
        const root = vscode.workspace.workspaceFolders?.[0]?.uri;
        return root ? { root, relFolder: raw } : null;
    }

    /** Core 에 넘길 (workspacePath, targetFolder). 절대경로 대상이면 그 폴더가 곧 프로젝트 루트다. */
    private _codeScope(targetFolder: string): { workspacePath: string; targetFolder: string } {
        const first = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        const raw = (targetFolder || '').trim();
        if (raw && (path.isAbsolute(raw) || raw.startsWith('~'))) {
            const desc = this._describeTargetFolder(raw);
            if (desc.insideWorkspace) { return { workspacePath: first, targetFolder: desc.display }; }
            return { workspacePath: desc.absolute, targetFolder: '' };
        }
        return { workspacePath: first, targetFolder: raw };
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
        const scope = this._codeScope(opts.targetFolder ?? '');
        const workspacePath = scope.workspacePath;
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
                targetFolder: scope.targetFolder,
            });
            this.postMessageToWebview(opts.requestWebview, 'code.planResult', {
                requestId: opts.requestId,
                instruction,
                decisions: plan.decisions ?? [],
                //: 걸러진 결정의 사유 — 웹뷰가 결정 모달에 표시한다.
                dropped: plan.dropped ?? [],
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
        const resolved = this._resolveWriteRoot(targetFolder);
        if (!resolved) {
            this.postMessage('code.error', { message: targetFolder && path.isAbsolute(targetFolder)
                ? `대상 폴더가 워크스페이스에 없습니다: ${targetFolder}. 승인 카드에서 다시 승인해 주세요.`
                : '워크스페이스가 열려있지 않습니다.', ...ack });
            return false;
        }
        const root = resolved.root;
        const combined = this._joinFolder(resolved.relFolder, file);
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

    /** docker-compose.yml 초안 생성 — 「인프라 파일 생성」 Compose 탭. */
    private async handleGenerateCompose(workspacePath: string, projectId?: string): Promise<void> {
        try {
            this._state.isLoading = true;
            this.postMessage('stateUpdate', this._state);
            const proposal = await this._apiClient.generateCompose(workspacePath, projectId);
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
        // `recoder-bot.png`은 4MB가 넘는 홍보용 자산이라 VSIX에서 제외한다.
        // 없는 파일에도 asWebviewUri를 만들면 ChatPanel은 비어 있지 않은 src를
        // 보고 <img>를 렌더해 설치본에서 깨진 아바타가 된다. 개발 환경처럼
        // 실제 파일이 있을 때만 URI를 주고, 패키지에서는 ChatPanel의 "R"
        // 기본 아바타를 쓰게 한다.
        const botAvatarPath = vscode.Uri.joinPath(
            this._extensionUri, 'media', 'recoder-bot.png'
        );
        const botAvatarUri = fs.existsSync(botAvatarPath.fsPath)
            ? webview.asWebviewUri(botAvatarPath)
            : '';
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
