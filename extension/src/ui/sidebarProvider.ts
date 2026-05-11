/**
 * Sidebar Webview Provider (설계서 v6.4 §12)
 * 3-Mode 탭 구조: Build | Ship | Operate
 * Extension ↔ Webview 메시지 브릿지
 *
 * 2026-05-08 갱신 (P0-7 / P0-8 / P0-13):
 * - server.py 응답 형식이 PatchProposal/InfraFileProposal 통째 반환으로 통일됨에 따라
 *   webview 의 파싱을 어댑터 한 군데로 정리.
 * - infra_approved 응답에 plan 이 함께 오므로 _currentDeployPlan 자동 세팅.
 * - /api/deploy/status polling, /api/security/scan, /api/ready 결선.
 * - Sidebar 상단 Ready 카드 (Core / AI / Docker 3-칩) 추가.
 * - S-4: 다중 파일 탭, S-5: 스테이지 바, S-7: 호출 횟수, S-8(TS): Git 커밋,
 *   S-9(TS): 롤백 버튼, S-10: Operate 탭 (준비 중)
 */
import * as vscode from 'vscode';
import * as crypto from 'crypto';
import { CoreManager } from '../core/coreManager';
import { ContextCollector } from '../collectors/contextCollector';
import { TerminalCollector } from '../collectors/terminalCollector';

function getNonce(): string {
    return crypto.randomBytes(16).toString('base64');
}

const DEPLOY_STATUS_POLL_MS = 1500;
const GH_AUTO_CONNECT_DISABLED_KEY = 'recoder.github.autoConnectDisabled';

export class SidebarProvider implements vscode.WebviewViewProvider {
    private _view?: vscode.WebviewView;
    private _contextCollector: ContextCollector;
    private _statusPollTimer: ReturnType<typeof setInterval> | null = null;
    private _readyPollTimer: ReturnType<typeof setInterval> | null = null;
    private _deployPollTimer: ReturnType<typeof setInterval> | null = null;
    private _gitInfoPollTimer: ReturnType<typeof setInterval> | null = null;
    // Workbench 연동: WorkbenchPanel 이 열릴 때 메시지 전송 함수를 등록
    private _workbenchSendFn: ((msg: object) => void) | null = null;
    // 중복 실행 방지 가드
    private _ghLoginInProgress = false;
    private _autoDetectInProgress = false;

    constructor(
        private readonly context: vscode.ExtensionContext,
        private readonly coreManager: CoreManager,
        private readonly terminalCollector: TerminalCollector,
    ) {
        this._contextCollector = new ContextCollector();
    }

    /** WorkbenchPanel 이 열릴 때 호출 — 브로드캐스트 함수 등록 */
    setWorkbenchSendFn(fn: ((msg: object) => void) | null): void {
        this._workbenchSendFn = fn;
    }

    /** Workbench 에서 온 메시지를 기존 핸들러로 라우팅 */
    async handleWorkbenchMessage(msg: any): Promise<void> {
        await this._handleMessage(msg);
    }

    resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken
    ): void {
        this._view = webviewView;
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this.context.extensionUri]
        };
        webviewView.webview.html = getWebviewHtml(webviewView.webview, this.context.extensionUri);

        webviewView.webview.onDidReceiveMessage(async (msg) => {
            await this._handleMessage(msg);
        });

        webviewView.onDidDispose(() => this._stopAllPolling());

        this._startStatusPolling();
        this._startReadyPolling();
        this._startGitInfoPolling();
    }

    /** 사이드바 + Workbench 모두에 메시지 브로드캐스트 */
    sendMessage(msg: object): void {
        try { this._view?.webview.postMessage(msg); } catch (_) { /* disposed */ }
        try { this._workbenchSendFn?.(msg); } catch (_) { /* disposed */ }
    }

    // ── Webview → Extension ────────────────────────────────────────

    private async _handleMessage(msg: { type: string; [key: string]: any }): Promise<void> {
        switch (msg.type) {
            case 'ready':
                await this._sendInitialState();
                break;

            case 'analyze':
                await this._handleAnalyze(msg);
                break;

            case 'approve_patch':
                await this._handleApprovePatch(msg);
                break;

            case 'reject_patch':
                await this._handleRejectPatch(msg);
                break;

            case 'generate_dockerfile':
                await this._handleGenerateInfra(msg);
                break;

            case 'approve_infra':
                await this._handleApproveInfra(msg);
                break;

            case 'deploy_local':
                await this._handleDeployLocal(msg);
                break;

            case 'run_security_scan':
                await this._handleSecurityScan(msg);
                break;

            case 'toggle_auto_detect':
                this.terminalCollector.setAutoDetect(!!msg.enabled);
                break;

            case 'scan_project':
                await this._handleScanProject();
                break;

            case 'deploy_rollback':
                await this._handleDeployRollback(msg);
                break;

            case 'deploy_ec2':
                await this._handleDeployEC2(msg);
                break;

            case 'deploy_ec2_status_poll':
                try {
                    const s = await this.coreManager.client.getEC2DeployStatus();
                    this.sendMessage({ type: 'ec2_deploy_status', data: s });
                } catch { /* 무시 */ }
                break;

            case 'deploy_ec2_ready_check':
                try {
                    const r = await this.coreManager.client.deployEC2Ready();
                    this.sendMessage({ type: 'ec2_ready_result', data: r });
                } catch (e: any) {
                    this.sendMessage({ type: 'ec2_ready_result', data: { ready: false, issues: [e?.message ?? String(e)] } });
                }
                break;

            case 'git_commit':
                await this._handleGitCommit(msg);
                break;

            case 'open_dashboard':
                await vscode.commands.executeCommand('recoder.openDashboard');
                break;

            case 'gh_status':
                await this._handleGhStatus(msg.force === true);
                break;

            case 'gh_login':
                await this._handleGhLogin();
                break;

            case 'gh_open_url':
                if (msg.url) {
                    await vscode.env.openExternal(vscode.Uri.parse(msg.url));
                }
                break;

            case 'gh_copy':
                if (msg.value) {
                    await vscode.env.clipboard.writeText(msg.value);
                    vscode.window.setStatusBarMessage('ReCoder: 클립보드에 복사되었습니다.', 2000);
                }
                break;

            case 'gh_install_hint':
                if (msg.hint) {
                    await vscode.env.clipboard.writeText(msg.hint);
                    vscode.window.showInformationMessage(`설치 명령어가 클립보드에 복사되었습니다: ${msg.hint}`);
                }
                break;

            case 'ship_github':
                await this._handleShipGitHub(msg);
                break;

            case 'gh_branches':
                await this._handleGhBranches();
                break;

            case 'gh_logout':
                await this._handleGhLogout();
                break;

            case 'gh_repos_request':
                await this._handleGhReposRequest();
                break;

            // ── Git GUI 패널 ─────────────────────────────────────────
            case 'git_info_request':
                // force=true 이면 원격 접근 가능 여부 캐시 무시 (UI에서 명시적 새로고침 시)
                await this._handleGitInfoRequest(msg.force === true);
                break;

            case 'git_branches_request':
                await this._handleGitBranchesRequest();
                break;

            case 'git_checkout':
                await this._handleGitCheckout(msg);
                break;

            case 'git_branch_create':
                await this._handleGitBranchCreate(msg);
                break;

            case 'git_set_remote':
                await this._handleGitSetRemote(msg);
                break;

            case 'git_push':
                await this._handleGitPush(msg);
                break;

            case 'git_commit_and_push':
                await this._handleGitCommitAndPush(msg);
                break;

            case 'open_workbench':
                await vscode.commands.executeCommand('recoder.openWorkbench');
                break;

            case 'open_workbench_page':
                // Workbench 열고 특정 탭으로 이동
                await vscode.commands.executeCommand('recoder.openWorkbench');
                // 짧은 딜레이 후 페이지 이동 메시지 전송
                setTimeout(() => {
                    this.sendMessage({ type: 'navigate_to_page', page: msg.page || 'command' });
                }, 300);
                break;

            case 'deploy_status_poll':
                // Workbench 가 배포 상태를 직접 폴링 요청
                try {
                    const ds = await this.coreManager.client.getDeployStatus();
                    this.sendMessage({ type: 'deploy_status', data: ds });
                } catch { /* 무시 */ }
                break;
        }
    }

    private async _handleGitInfoRequest(force = false): Promise<void> {
        try {
            const ctx: any = this._contextCollector.collect();
            const workspacePath = ctx.workspace_path ?? '';
            const result = await this.coreManager.client.gitInfo(workspacePath, force);
            this.sendMessage({ type: 'git_info_result', data: result });
        } catch (e: any) {
            this.sendMessage({ type: 'git_info_result', data: { status: 'error', is_git_repo: false, branch: '', message: e?.message } });
        }
    }

    private async _handleGitBranchesRequest(): Promise<void> {
        try {
            const ctx: any = this._contextCollector.collect();
            const workspacePath = ctx.workspace_path ?? '';
            const result = await this.coreManager.client.gitBranches(workspacePath);
            this.sendMessage({ type: 'git_branches_result', data: result });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `브랜치 목록 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleGitCheckout(msg: any): Promise<void> {
        try {
            const ctx: any = this._contextCollector.collect();
            const workspacePath = ctx.workspace_path ?? '';
            const result = await this.coreManager.client.gitCheckout(workspacePath, msg.branch);
            this.sendMessage({ type: 'git_checkout_result', data: result });
            if (result.status === 'ok') {
                vscode.window.showInformationMessage(`ReCoder: 브랜치 전환 — ${result.branch}`);
                // 전환 후 git info 새로고침
                await this._handleGitInfoRequest();
            } else {
                vscode.window.showWarningMessage(`ReCoder: 브랜치 전환 실패 — ${result.message}`);
            }
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `브랜치 전환 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleGitBranchCreate(msg: any): Promise<void> {
        try {
            const ctx: any = this._contextCollector.collect();
            const workspacePath = ctx.workspace_path ?? '';
            const result = await this.coreManager.client.gitBranchCreate(
                workspacePath, msg.branch_name, msg.checkout !== false,
            );
            this.sendMessage({ type: 'git_branch_create_result', data: result });
            if (result.status === 'ok') {
                vscode.window.showInformationMessage(`ReCoder: 브랜치 생성 — ${result.branch}`);
                await this._handleGitInfoRequest();
                await this._handleGitBranchesRequest();
            } else {
                vscode.window.showWarningMessage(`ReCoder: 브랜치 생성 실패 — ${result.message}`);
            }
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `브랜치 생성 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleGitSetRemote(msg: any): Promise<void> {
        try {
            const ctx: any = this._contextCollector.collect();
            const workspacePath = ctx.workspace_path ?? '';
            const result = await this.coreManager.client.gitSetRemote(workspacePath, msg.repo_full_name);
            this.sendMessage({ type: 'git_set_remote_result', data: result });
            if (result.status === 'ok') {
                vscode.window.showInformationMessage(`ReCoder: 원격 저장소 변경 → ${msg.repo_full_name}`);
                // 변경 후 즉시 git info 강제 새로고침
                await this._handleGitInfoRequest(true);
            } else {
                vscode.window.showWarningMessage(`ReCoder: 원격 저장소 변경 실패 — ${result.message}`);
            }
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `원격 저장소 변경 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleGitPush(msg: any): Promise<void> {
        try {
            const ctx: any = this._contextCollector.collect();
            const workspacePath = ctx.workspace_path ?? '';
            const result = await this.coreManager.client.gitPush(workspacePath, msg.branch ?? '', msg.force ?? false);
            this.sendMessage({ type: 'git_push_result', data: result });
            if (result.status === 'ok') {
                vscode.window.showInformationMessage(`ReCoder: Push 완료.`);
            } else if (result.status === 'no_remote') {
                // remote 없음 → Ship 탭으로 안내
                this.sendMessage({ type: 'git_push_no_remote' });
            } else {
                vscode.window.showWarningMessage(`ReCoder: Push 실패 — ${result.message}`);
            }
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `Push 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleGitCommitAndPush(msg: any): Promise<void> {
        try {
            const ctx: any = this._contextCollector.collect();
            const workspacePath = ctx.workspace_path ?? '';
            const commitMsg = msg.message || `ReCoder: auto-commit`;

            // 1) 커밋
            const commitResult = await this.coreManager.client.gitCommit(workspacePath, commitMsg);
            this.sendMessage({ type: 'git_commit_result', data: commitResult });

            if (commitResult.status !== 'ok') {
                vscode.window.showWarningMessage(`ReCoder: 커밋 실패 — ${commitResult.message}`);
                return;
            }

            if (msg.push !== true) {
                vscode.window.showInformationMessage(`ReCoder: commit complete.`);
                return;
            }

            // 2) Push
            const pushResult = await this.coreManager.client.gitPush(workspacePath, '', false);
            this.sendMessage({ type: 'git_push_result', data: pushResult });

            if (pushResult.status === 'ok') {
                vscode.window.showInformationMessage(`ReCoder: 커밋 및 Push 완료.`);
            } else if (pushResult.status === 'no_remote') {
                this.sendMessage({ type: 'git_push_no_remote' });
                vscode.window.showInformationMessage(`ReCoder: 커밋 완료. Push할 원격 저장소를 설정하세요.`);
            } else {
                vscode.window.showWarningMessage(`ReCoder: Push 실패 — ${pushResult.message}`);
            }
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `커밋 & Push 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleGhBranches(): Promise<void> {
        try {
            const ctx: any = this._contextCollector.collect();
            const result = await this.coreManager.client.ghBranches(ctx.workspace_path ?? '');
            this.sendMessage({ type: 'gh_branches_result', data: result });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `브랜치 목록 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleGhLogout(): Promise<void> {
        try {
            const client = await this.coreManager.ensureRunning();
            await this.context.globalState.update(GH_AUTO_CONNECT_DISABLED_KEY, true);
            await client.ghLogout();
            const st = await client.ghStatus(true);
            this.sendMessage({ type: 'gh_status_result', data: st });
            this.sendMessage({ type: 'gh_login_progress', message: '' });
            vscode.window.showInformationMessage('ReCoder: GitHub 연결을 해제했습니다. 다시 연결 버튼을 누르기 전까지 자동 재연결하지 않습니다.');
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `로그아웃 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleGhReposRequest(): Promise<void> {
        try {
            const result = await this.coreManager.client.ghRepos();
            this.sendMessage({ type: 'gh_repos_result', data: result });
        } catch (e: any) {
            this.sendMessage({ type: 'gh_repos_result', data: { status: 'error', repos: [], message: e?.message ?? String(e) } });
        }
    }

    private async _handleGhStatus(force: boolean = false): Promise<void> {
        try {
            const client = await this.coreManager.ensureRunning();
            const st = await client.ghStatus(force);
            this.sendMessage({ type: 'gh_status_result', data: st });
            // Core 가 미인증 상태인데 VS Code 에 이미 GitHub 계정이 연동돼 있다면
            // 사용자 조작 없이 토큰을 동기화 (P0 버그: 이미 연동된 계정이 안 뜨는 문제)
            const autoConnectDisabled = this.context.globalState.get<boolean>(GH_AUTO_CONNECT_DISABLED_KEY, false);
            if (!st?.authed && !autoConnectDisabled) {
                void this._autoDetectGhSession();
            }
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `gh 상태 확인 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleGhLogin(): Promise<void> {
        // 동시 로그인 시도 차단 — 여러 소스에서 gh_login 이 동시에 오면 팝업이 중복 표시됨
        if (this._ghLoginInProgress) {
            return;
        }
        this._ghLoginInProgress = true;

        const reportError = (message: string): void => {
            this.sendMessage({ type: 'error', message });
            this.sendMessage({ type: 'gh_login_progress', message: '' });
            vscode.window.showWarningMessage(`ReCoder: ${message}`);
        };

        try {
            await this.context.globalState.update(GH_AUTO_CONNECT_DISABLED_KEY, false);
            this.sendMessage({ type: 'gh_login_progress', message: 'GitHub 인증 창을 엽니다...' });

            // Core 가 살아있는지 먼저 확인 — 죽어 있으면 사용자에게 명확히 안내
            let client;
            try {
                client = await this.coreManager.ensureRunning();
            } catch (coreErr: any) {
                reportError(`Core 미실행: ${coreErr?.message ?? coreErr}`);
                return;
            }

            const scopes = ['repo', 'read:org', 'workflow'];
            let session: vscode.AuthenticationSession | undefined;
            try {
                // 1) 팝업 없이 캐시된 세션 재사용 (로그아웃 후 재연결 시 팝업 방지)
                session = await vscode.authentication.getSession(
                    'github', scopes,
                    { createIfNone: false, silent: true } as any,
                );
                // 2) 캐시된 세션이 없을 때만 팝업 표시 (최초 1회)
                if (!session?.accessToken) {
                    session = await vscode.authentication.getSession('github', scopes, { createIfNone: true });
                }
            } catch (authErr: any) {
                // 사용자가 팝업을 닫거나 취소한 경우 — 에러 없이 조용히 종료
                const msg: string = authErr?.message ?? String(authErr);
                if (
                    msg.toLowerCase().includes('cancel') ||
                    msg.toLowerCase().includes('did not consent') ||
                    msg.toLowerCase().includes('user cancelled')
                ) {
                    this.sendMessage({ type: 'gh_login_progress', message: '' });
                    return;
                }
                reportError(`GitHub 인증 실패: ${msg}`);
                return;
            }

            if (!session?.accessToken) {
                // 사용자가 인증 흐름 도중 취소
                this.sendMessage({ type: 'gh_login_progress', message: '' });
                return;
            }

            this.sendMessage({ type: 'gh_login_progress', message: 'GitHub 토큰 검증 중...' });

            // Core 에 토큰 전달 (유효성 검증 + 사용자명 캐싱 포함)
            const result = await client.ghSetToken(session.accessToken);

            if (result.status === 'ok' && result.user) {
                vscode.window.showInformationMessage(`ReCoder: GitHub 연결 완료 (${result.user})`);
                const st = await client.ghStatus(true);
                this.sendMessage({ type: 'gh_status_result', data: st });
                this.sendMessage({ type: 'gh_login_progress', message: '' });
            } else {
                reportError(`GitHub 연결 실패 — ${result.message ?? '토큰 검증 실패'}`);
            }

        } catch (e: any) {
            reportError(`GitHub 연결 실패: ${e?.message ?? e}`);
        } finally {
            this._ghLoginInProgress = false;
        }
    }

    /**
     * 시작 시 / GitHub 탭 진입 시 호출 — VS Code 에 이미 로그인된 GitHub 계정을 감지.
     *
     * VS Code 의 GitHub 인증은 "스코프(scope) 단위 토큰 캐시" 방식이라서,
     * Accounts 메뉴에서 GitHub 로그인을 했더라도 ReCoder 가 요구하는 스코프
     * (repo / read:org / workflow) 로 발급된 토큰이 캐시에 없으면 silent 호출은 빈 결과를 돌려준다.
     *
     * 2026-05-11 (P0 후속): 2단계 감지로 변경.
     *   1) 요구 스코프로 silent 시도 → 성공하면 Core 와 토큰 동기화
     *   2) 빈 스코프로 silent 시도 → 성공하면 "VS Code 계정은 있지만 권한 부여 필요" 상태로 UI 표시
     *
     * `silent: true` 옵션을 사용해 인증 팝업이 절대 뜨지 않도록 보장한다.
     * 세션이 없거나 Core 가 죽어 있으면 조용히 무시 (사용자가 명시적으로 "연결" 버튼을 누를 때까지 대기).
     */
    private async _autoDetectGhSession(): Promise<void> {
        // 동시 실행 차단 — 여러 gh_status 폴링이 동시에 이 함수를 호출하면 중복 팝업 발생
        if (this._autoDetectInProgress) {
            return;
        }
        this._autoDetectInProgress = true;
        try {
            if (this.context.globalState.get<boolean>(GH_AUTO_CONNECT_DISABLED_KEY, false)) {
                return;
            }
            const scopes = ['repo', 'read:org', 'workflow'];

            // 1단계: 요구 스코프로 캐시된 세션 확인
            let session: vscode.AuthenticationSession | undefined;
            try {
                session = await vscode.authentication.getSession(
                    'github',
                    scopes,
                    { createIfNone: false, silent: true } as any,
                );
            } catch { /* 무시 */ }

            if (session?.accessToken) {
                // Core 가 실행 중이고 아직 인증 안 되어 있을 때만 토큰 전달
                let client;
                try { client = await this.coreManager.ensureRunning(); } catch { return; }

                try {
                    const cur = await client.ghStatus(false);
                    if (cur?.authed && cur?.user) {
                        this.sendMessage({ type: 'gh_status_result', data: cur });
                        return;
                    }
                } catch { /* status 조회 실패 시 토큰 전달 시도 */ }

                try {
                    const result = await client.ghSetToken(session.accessToken);
                    if (result.status === 'ok' && result.user) {
                        const st = await client.ghStatus(true);
                        this.sendMessage({ type: 'gh_status_result', data: st });
                        return;
                    }
                } catch (_) { /* 자동 흐름 — 조용히 무시 */ }
                return;
            }

            // 2단계: VS Code 의 GitHub 계정 "존재 여부" 만 빈 스코프로 감지
            //         (스코프 불일치로 1단계가 빈 결과를 줬을 수 있음)
            let basicSession: vscode.AuthenticationSession | undefined;
            try {
                basicSession = await vscode.authentication.getSession(
                    'github',
                    [],
                    { createIfNone: false, silent: true } as any,
                );
            } catch { /* 무시 */ }

            if (basicSession?.account?.label) {
                this.sendMessage({
                    type: 'gh_status_result',
                    data: {
                        installed: true,
                        version: '',
                        authed: false,
                        user: '',
                        install_hint: '',
                        vscode_detected: true,
                        vscode_user: basicSession.account.label,
                    },
                });
            }
        } catch (_) { /* getSession 자체가 실패할 수 있음 — 무시 */ }
        finally {
            this._autoDetectInProgress = false;
        }
    }

    private async _handleShipGitHub(msg: any): Promise<void> {
        try {
            const ctx: any = this._contextCollector.collect();
            // workspace_path 없으면 Core의 스캔 결과를 폴백으로 사용
            let workspacePath = ctx.workspace_path ?? '';
            if (!workspacePath) {
                try {
                    const proj: any = await this.coreManager.client.getProject();
                    workspacePath = proj?.workspace_path ?? '';
                } catch { /* 무시 */ }
            }
            if (!workspacePath) {
                this.sendMessage({ type: 'error', message: 'Ship 실행 실패: VS Code에서 배포할 프로젝트 폴더를 먼저 열어주세요.' });
                return;
            }
            const includeInfra = msg.include_dockerfile !== false
                || msg.include_compose !== false
                || msg.include_actions !== false
                || msg.include_dockerignore !== false;
            try {
                this.sendMessage({ type: 'project_scan_started' });
                const project = await this.coreManager.client.scanProject(workspacePath);
                this.sendMessage({ type: 'project_scanned', data: project });
            } catch (scanError: any) {
                if (includeInfra) {
                    throw new Error(`프로젝트 스캔 실패: ${scanError?.message ?? scanError}`);
                }
                this.sendMessage({
                    type: 'error',
                    message: `프로젝트 스캔 실패, 인프라 생성 없이 Ship을 계속합니다: ${scanError?.message ?? scanError}`,
                });
            }
            const payload = {
                workspace_path: workspacePath,
                repo_name: msg.repo_name ?? '',
                private: msg.private !== false,
                description: msg.description ?? '',
                secrets: msg.secrets ?? {},
                include_dockerfile: msg.include_dockerfile !== false,
                include_compose: msg.include_compose !== false,
                include_actions: msg.include_actions !== false,
                include_dockerignore: msg.include_dockerignore !== false,
            };
            const r = await this.coreManager.client.shipGitHub(payload);
            this.sendMessage({ type: 'ship_github_started', data: r });
            const tick = async (): Promise<void> => {
                try {
                    const s = await this.coreManager.client.shipGitHubStatus();
                    this.sendMessage({ type: 'ship_github_status', data: s });
                    if (!s.running) { return; }
                    setTimeout(tick, 1500);
                } catch { setTimeout(tick, 3000); }
            };
            setTimeout(tick, 1000);
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `Ship 실행 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleAnalyze(msg: any): Promise<void> {
        try {
            await this.coreManager.ensureRunning();
            const ctx: any = this._contextCollector.collect();
            const proposal = await this.coreManager.client.analyze({
                workspace_path: ctx.workspace_path ?? '',
                terminal_output: msg.terminal_output ?? '',
                active_file_path: ctx.active_file_path ?? '',
                selected_text: ctx.selected_text ?? '',
                command: ctx.command ?? '',
                project_files_summary: ctx.project_files_summary ?? '',
                error_text: msg.error_text ?? '',
                file_context: ctx.file_context ?? '',
                related_files: ctx.related_files ?? [],
            });
            this.sendMessage({ type: 'analyze_result', data: proposal });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: e?.message ?? String(e) });
        }
    }

    private async _handleApprovePatch(msg: any): Promise<void> {
        try {
            const result = await this.coreManager.client.approvePatch(msg.proposal_id);
            this.sendMessage({ type: 'patch_result', data: result });
            this.sendMessage({ type: 'patch_approved', data: result });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `패치 적용 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleRejectPatch(msg: any): Promise<void> {
        try {
            await this.coreManager.client.rejectPatch(msg.proposal_id);
            this.sendMessage({ type: 'patch_rejected' });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: e?.message ?? String(e) });
        }
    }

    private async _handleGenerateInfra(msg: any): Promise<void> {
        try {
            await this.coreManager.ensureRunning();
            const ctx: any = this._contextCollector.collect();
            const fileType = (msg.file_type ?? 'dockerfile').toLowerCase();
            const proposal = await this.coreManager.client.generateInfra(
                msg.project_id ?? '',
                fileType,
                ctx.workspace_path ?? '',
            );
            this.sendMessage({ type: 'dockerfile_result', data: proposal });
            this.sendMessage({ type: 'infra_generated', data: proposal });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `Dockerfile 생성 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleApproveInfra(msg: any): Promise<void> {
        try {
            const result = await this.coreManager.client.approveInfra(msg.proposal_id);
            this.sendMessage({ type: 'infra_approved', data: result });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `파일 저장 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleDeployLocal(msg: any): Promise<void> {
        try {
            const result = await this.coreManager.client.deployLocal({
                plan_id: msg.plan_id ?? '',
            });
            this.sendMessage({ type: 'deploy_started', data: result });
            this._startDeployPolling();
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `배포 시작 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleSecurityScan(msg: any): Promise<void> {
        try {
            const result = await this.coreManager.client.runSecurityScan(
                msg.image ?? 'recoder-app:latest',
                msg.dockerfile_path ?? '',
            );
            this.sendMessage({ type: 'security_scan_result', data: result });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `보안 스캔 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleDeployEC2(msg: any): Promise<void> {
        try {
            await this.coreManager.ensureRunning();
            const ctx: any = this._contextCollector.collect();
            const workspacePath = msg.workspace_path || ctx.workspace_path || '';

            const payload = {
                workspace_path:    workspacePath,
                image_name:        msg.image_name        || 'recoder-app',
                repo_name:         msg.repo_name         || 'recoder-app',
                tag:               msg.tag               || 'latest',
                container_name:    msg.container_name    || 'recoder-app',
                host_port:         msg.host_port         || 8000,
                container_port:    msg.container_port    || 8000,
                health_check_path: msg.health_check_path || '/health',
                env_vars:          msg.env_vars          || [],
                ecr_registry:      msg.ecr_registry      || '',
                ec2_host:          msg.ec2_host          || '',
                ec2_ssh_key:       msg.ec2_ssh_key        || '',
                aws_region:        msg.aws_region        || '',
                ec2_user:          msg.ec2_user          || 'ec2-user',
            };

            const result = await this.coreManager.client.deployEC2(payload);
            this.sendMessage({ type: 'ec2_deploy_started', data: result });

            if (result.status === 'ok') {
                vscode.window.showInformationMessage('ReCoder: EC2 배포 시작됨.');
                // 폴링 시작
                const tick = async (): Promise<void> => {
                    try {
                        const s = await this.coreManager.client.getEC2DeployStatus();
                        this.sendMessage({ type: 'ec2_deploy_status', data: s });
                        if (s.running) { setTimeout(tick, 2000); }
                        else if (s.stage === 'done') {
                            vscode.window.showInformationMessage(`ReCoder: EC2 배포 완료 ✓`);
                        } else if (s.stage === 'failed') {
                            vscode.window.showErrorMessage(`ReCoder: EC2 배포 실패 — ${s.error}`);
                        }
                    } catch { setTimeout(tick, 3000); }
                };
                setTimeout(tick, 1500);
            } else {
                vscode.window.showErrorMessage(`ReCoder: EC2 배포 시작 실패 — ${result.message}`);
            }
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `EC2 배포 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleDeployRollback(msg: any): Promise<void> {
        try {
            const result = await this.coreManager.client.deployRollback(msg.plan_id ?? '');
            this.sendMessage({ type: 'rollback_result', data: result });
            if (result.status === 'ok') {
                vscode.window.showInformationMessage('ReCoder: 롤백이 완료되었습니다.');
            } else {
                vscode.window.showWarningMessage(`ReCoder: 롤백 실패 — ${result.message}`);
            }
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `롤백 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleGitCommit(msg: any): Promise<void> {
        try {
            const ctx: any = this._contextCollector.collect();
            const workspacePath = ctx.workspace_path ?? '';
            const proposalId = msg.proposal_id ?? '';
            const commitMsg = msg.message || `ReCoder: auto-patch by AI (${proposalId.slice(0, 8)})`;
            const result = await this.coreManager.client.gitCommit(workspacePath, commitMsg);
            this.sendMessage({ type: 'git_commit_result', data: result });
            if (result.status === 'ok') {
                vscode.window.showInformationMessage(`ReCoder: 커밋 완료 (${result.commit_hash.slice(0, 8)})`);
            } else {
                vscode.window.showWarningMessage(`ReCoder: 커밋 실패 — ${result.message}`);
            }
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `Git 커밋 실패: ${e?.message ?? e}` });
        }
    }

    private async _handleScanProject(): Promise<void> {
        try {
            const ctx: any = this._contextCollector.collect();
            const workspacePath = ctx.workspace_path ?? '';
            if (!workspacePath) {
                this.sendMessage({ type: 'error', message: '프로젝트 스캔 실패: VS Code에서 프로젝트 폴더를 먼저 열어주세요.' });
                return;
            }
            this.sendMessage({ type: 'project_scan_started' });
            const result = await this.coreManager.client.scanProject(workspacePath);
            this.sendMessage({ type: 'project_scanned', data: result });
        } catch (e: any) {
            this.sendMessage({ type: 'error', message: `프로젝트 스캔 실패: ${e?.message ?? e}` });
        }
    }

    // ── 초기 상태 / 폴링 ───────────────────────────────────────────

    private async _sendInitialState(): Promise<void> {
        try {
            const client = await this.coreManager.ensureRunning();
            const [cost, ready] = await Promise.all([
                client.getCost().catch(() => null),
                client.getReady().catch(() => null),
            ]);
            if (ready) {
                this.sendMessage({ type: 'ready_update', ready });
            }
            if (cost) {
                this.sendMessage({ type: 'cost_update', data: cost });
            }
            // 시작 시 VS Code 에 이미 로그인된 GitHub 계정을 조용히 감지 → Core 와 동기화
            void this._autoDetectGhSession();
        } catch {
            this.sendMessage({ type: 'core_offline' });
        }
    }

    private _startStatusPolling(): void {
        this._statusPollTimer = setInterval(async () => {
            try {
                const status = await this.coreManager.client.getStatus();
                // 'data' 키로 통일 — sidebar.js 의 msg.data 와 일치
                this.sendMessage({ type: 'status_update', data: status });
                const cost = await this.coreManager.client.getCost().catch(() => null);
                if (cost) { this.sendMessage({ type: 'cost_update', data: cost }); }
            } catch { /* core offline */ }
        }, 4000);
    }

    private _startReadyPolling(): void {
        this._readyPollTimer = setInterval(async () => {
            try {
                const ready = await this.coreManager.client.getReady();
                this.sendMessage({ type: 'ready_update', ready });
            } catch { /* core offline */ }
        }, 8000);
    }

    private _startDeployPolling(): void {
        if (this._deployPollTimer) { return; }
        this._deployPollTimer = setInterval(async () => {
            try {
                const status = await this.coreManager.client.getDeployStatus();
                this.sendMessage({ type: 'deploy_status', data: status });
                if (status.finished) {
                    this._stopDeployPolling();
                }
            } catch { /* keep polling */ }
        }, DEPLOY_STATUS_POLL_MS);
    }

    private _stopDeployPolling(): void {
        if (this._deployPollTimer) {
            clearInterval(this._deployPollTimer);
            this._deployPollTimer = null;
        }
    }

    private _startGitInfoPolling(): void {
        // 초기 1회 즉시 실행
        setTimeout(() => this._handleGitInfoRequest(), 1000);
        // 이후 12초마다 폴링
        this._gitInfoPollTimer = setInterval(async () => {
            await this._handleGitInfoRequest();
        }, 12000);
    }

    private _stopAllPolling(): void {
        if (this._statusPollTimer) { clearInterval(this._statusPollTimer); }
        if (this._readyPollTimer) { clearInterval(this._readyPollTimer); }
        if (this._gitInfoPollTimer) { clearInterval(this._gitInfoPollTimer); }
        this._stopDeployPolling();
        this._statusPollTimer = null;
        this._readyPollTimer = null;
        this._gitInfoPollTimer = null;
    }
}



function getWebviewHtml(webview: vscode.Webview, extensionUri: vscode.Uri): string {
  const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, 'media', 'sidebar.js'));
  const nonce = getNonce();
  return `<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<meta name="color-scheme" content="dark">
<title>ReCoder</title>
<style>
:root{
  --bg0:#0d1117;--bg1:#161b22;--bg2:#21262d;--bg3:#30363d;
  --bd:#30363d;--bd2:#484f58;
  --t1:#e6edf3;--t2:#8b949e;--t3:#6e7681;
  --blue:#58a6ff;--blue-bg:rgba(88,166,255,.08);
  --green:#3fb950;--green-bg:rgba(63,185,80,.08);
  --red:#f85149;--red-bg:rgba(248,81,73,.08);
  --yellow:#d29922;--yellow-bg:rgba(210,153,34,.08);
  --radius-sm:4px;--radius-md:6px;--radius-lg:10px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg0);color:var(--t1);
  font-family:'Malgun Gothic','Apple SD Gothic Neo','Noto Sans KR',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  font-size:12px;overflow-x:hidden;
  padding-bottom:36px;
}

/* ── Ready chips ── */
.ready-bar{
  display:flex;align-items:center;gap:4px;
  padding:7px 10px;background:var(--bg1);border-bottom:1px solid var(--bd);
  flex-wrap:wrap;
}
.rc{
  display:flex;align-items:center;gap:3px;
  padding:2px 6px;border-radius:var(--radius-lg);border:1px solid var(--bd);
  font-size:10px;font-weight:600;color:var(--t3);cursor:default;flex:1;justify-content:center;
}
.rc .dot{width:5px;height:5px;border-radius:50%;background:var(--t3);flex-shrink:0}
.rc.ok{color:var(--green);border-color:rgba(63,185,80,.35)}.rc.ok .dot{background:var(--green)}
.rc.warn{color:var(--yellow);border-color:rgba(210,153,34,.35)}.rc.warn .dot{background:var(--yellow)}
.rc.fail{color:var(--red);border-color:rgba(248,81,73,.35)}.rc.fail .dot{background:var(--red)}

/* ── Section label ── */
.sec-label{
  font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
  color:var(--t3);padding:8px 10px 3px;
}

/* ── Mini cards ── */
.mini-card{
  margin:2px 8px;padding:8px 10px;
  background:var(--bg1);border:1px solid var(--bd);border-radius:var(--radius-md);
}

/* ── Issue card ── */
.issue-dot{
  display:inline-block;width:7px;height:7px;border-radius:50%;
  background:var(--red);margin-right:4px;vertical-align:middle;
}
.issue-summary{font-size:11px;color:var(--t1);line-height:1.4;margin-bottom:3px}
.issue-time{font-size:10px;color:var(--t3)}
.no-issue{font-size:11px;color:var(--t3);display:flex;align-items:center;gap:5px}

/* ── GitHub card ── */
.gh-card-row{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--t2);padding:2px 0}
.gh-card-row .icon{font-size:12px;flex-shrink:0;width:14px;text-align:center}
.gh-card-row .val{color:var(--t1);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#sb-github-card.linked .gh-user-val{color:var(--green)}

/* ── Deploy card ── */
.sb-deploy-row{display:flex;align-items:center;justify-content:space-between;font-size:11px}
.sb-health-badge{
  padding:1px 6px;border-radius:var(--radius-sm);font-size:10px;font-weight:600;
  background:var(--bg3);color:var(--t3);
}
.sb-health-badge.ok{background:var(--green-bg);color:var(--green)}

/* ── Quick launch buttons ── */
.quick-btns{display:flex;flex-direction:column;gap:0;padding:2px 8px 4px}
.quick-btn{
  display:flex;align-items:center;gap:8px;
  padding:7px 10px;border-radius:var(--radius-md);
  border:1px solid var(--bd);background:var(--bg1);color:var(--t1);
  font-size:11px;font-weight:500;cursor:pointer;transition:all .15s;text-align:left;width:100%;
  margin-bottom:4px;
}
.quick-btn:hover{background:var(--bg2);border-color:var(--bd2)}
.quick-btn.active{
  border-radius:var(--radius-md) var(--radius-md) 0 0;
  border-color:var(--blue);background:var(--blue-bg);color:var(--blue);
  margin-bottom:0;
}
.quick-btn .q-icon{font-size:14px;flex-shrink:0}
.quick-btn .q-label{flex:1}
.quick-btn .q-badge{
  font-size:9px;padding:1px 5px;border-radius:3px;
  background:var(--red-bg);color:var(--red);font-weight:700;
}
.quick-btn .q-badge.ok{background:var(--green-bg);color:var(--green)}
.quick-btn .q-arrow{font-size:9px;color:var(--t3);transition:transform .2s;flex-shrink:0}
.quick-btn.active .q-arrow{transform:rotate(90deg);color:var(--blue)}

/* ── Inline panels (accordion) ── */
.sb-panel{
  display:none;
  border:1px solid var(--blue);border-top:none;
  border-radius:0 0 var(--radius-md) var(--radius-md);
  background:var(--bg1);padding:8px;
  margin-bottom:4px;
}
.sb-panel.open{display:block}
.sb-panel-section{margin-bottom:6px}
.sb-panel-label{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--t3);margin-bottom:3px}
.sb-panel-textarea{
  width:100%;background:var(--bg2);color:var(--t1);border:1px solid var(--bd);
  border-radius:var(--radius-sm);padding:4px 7px;font-size:10px;outline:none;
  resize:none;min-height:44px;font-family:inherit;line-height:1.5;
}
.sb-panel-textarea:focus{border-color:var(--blue)}
.sb-panel-textarea::placeholder{color:var(--t3)}
.sb-panel-btn{
  width:100%;padding:5px 0;border-radius:var(--radius-sm);font-size:10px;font-weight:600;
  cursor:pointer;border:1px solid var(--bd);background:var(--bg2);color:var(--t1);
  transition:all .15s;margin-top:4px;
}
.sb-panel-btn:hover{background:var(--bg3)}
.sb-panel-btn.primary{border-color:rgba(88,166,255,.5);background:var(--blue-bg);color:var(--blue)}
.sb-panel-btn.primary:hover{background:rgba(88,166,255,.18)}
.sb-panel-btn.success{border-color:rgba(63,185,80,.5);background:var(--green-bg);color:var(--green)}
.sb-panel-btn.success:hover{background:rgba(63,185,80,.18)}
.sb-panel-btn.danger{border-color:rgba(248,81,73,.4);background:var(--red-bg);color:var(--red)}
.sb-panel-btn:disabled{opacity:.5;cursor:default}
.sb-panel-row{display:flex;gap:4px}
.sb-panel-row .sb-panel-btn{flex:1}
.sb-result{
  margin-top:5px;padding:6px 8px;background:var(--bg2);border:1px solid var(--bd);
  border-radius:var(--radius-sm);font-size:10px;color:var(--t2);line-height:1.5;
  display:none;
}
.sb-result.show{display:block}
.sb-result.ok{border-color:rgba(63,185,80,.35);color:var(--t1)}
.sb-result.fail{border-color:rgba(248,81,73,.35);color:var(--red)}
.sb-result-title{font-weight:700;color:var(--t1);margin-bottom:3px;font-size:10px}
.sb-panel-status{font-size:9px;color:var(--t3);margin-top:3px;min-height:11px}
.sb-panel-divider{height:1px;background:var(--bd);margin:6px 0}
.sb-panel-footer{
  margin-top:6px;padding-top:5px;border-top:1px solid var(--bd);
  display:flex;justify-content:flex-end;
}
.sb-wb-link{
  display:flex;align-items:center;gap:3px;
  font-size:9px;color:var(--t3);cursor:pointer;border:none;background:none;
  padding:2px 4px;border-radius:3px;
}
.sb-wb-link:hover{color:var(--blue);background:var(--blue-bg)}
/* deploy status rows */
.sb-dep-row{display:flex;justify-content:space-between;align-items:center;font-size:10px;margin-bottom:3px}
.sb-dep-row .key{color:var(--t3)}.sb-dep-row .val{color:var(--t1);font-weight:500}
.sb-dep-badge{padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700;background:var(--bg3);color:var(--t3)}
.sb-dep-badge.ok{background:var(--green-bg);color:var(--green)}
.sb-dep-badge.running{background:var(--blue-bg);color:var(--blue)}

/* ── Workbench open button ── */
.wb-open-btn{
  display:flex;align-items:center;justify-content:center;gap:8px;
  width:calc(100% - 16px);margin:8px;
  padding:10px 0;border-radius:var(--radius-md);
  border:1px solid rgba(88,166,255,.5);
  background:var(--blue-bg);color:var(--blue);
  font-size:12px;font-weight:700;cursor:pointer;transition:all .15s;
}
.wb-open-btn:hover{background:rgba(88,166,255,.18);transform:translateY(-1px)}
.wb-open-btn:active{transform:translateY(0)}

/* ── Git mini strip ── */
.git-strip{
  background:var(--bg1);border-top:1px solid var(--bd);border-bottom:1px solid var(--bd);
  padding:6px 8px;
}
.git-row1{display:flex;align-items:center;gap:4px;flex-wrap:wrap;margin-bottom:5px}
.git-acct{
  display:flex;align-items:center;gap:3px;padding:2px 6px;border-radius:var(--radius-lg);
  border:1px solid var(--bd);font-size:10px;color:var(--t3);background:var(--bg2);
}
.git-acct.ok{color:var(--green);border-color:rgba(63,185,80,.35);background:var(--green-bg)}
.git-branch-pill{
  display:flex;align-items:center;gap:3px;padding:2px 7px;border-radius:var(--radius-lg);
  border:1px solid rgba(88,166,255,.35);background:var(--blue-bg);color:var(--blue);
  font-size:10px;cursor:pointer;flex:1;
}
.git-branch-pill:hover{background:rgba(88,166,255,.15)}
.bname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:70px}
.git-chip{padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700}
.git-chip-changed{background:var(--yellow-bg);color:var(--yellow)}
.git-chip-ahead{background:var(--blue-bg);color:var(--blue)}
.git-row2{display:flex;gap:5px}
.git-row2 button{
  flex:1;padding:4px 0;border-radius:var(--radius-sm);font-size:10px;
  font-weight:600;cursor:pointer;border:1px solid var(--bd);background:var(--bg2);color:var(--t1);transition:all .15s;
}
.git-row2 button:hover{background:var(--bg3)}

/* ── Git dropdown ── */
.git-dropdown{display:none;border-top:1px solid var(--bd);padding:5px 6px;margin-top:5px}
.git-dropdown.open{display:block}
.gd-section{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--t3);margin-bottom:3px;padding:0 2px}
.gd-branch{display:flex;align-items:center;gap:5px;padding:3px 5px;border-radius:var(--radius-sm);font-size:10px;cursor:pointer}
.gd-branch:hover{background:var(--bg2)}
.gd-branch.current{color:var(--green);font-weight:600}
.gd-dot{width:5px;height:5px;border-radius:50%;background:var(--bd2);flex-shrink:0}
.gd-dot.current{background:var(--green)}
.gd-remote-tag{margin-left:auto;font-size:8px;padding:1px 3px;border-radius:3px;background:var(--bg3);color:var(--t3)}
.gd-new-row{display:flex;gap:4px;margin-top:5px}
.gd-new-input{flex:1;background:var(--bg2);color:var(--t1);border:1px solid var(--bd);border-radius:var(--radius-sm);padding:3px 6px;font-size:10px;outline:none}
.gd-new-input:focus{border-color:var(--blue)}
.gd-new-btn{padding:3px 7px;border-radius:var(--radius-sm);border:1px solid rgba(88,166,255,.4);color:var(--blue);background:var(--blue-bg);cursor:pointer;font-size:10px;white-space:nowrap}

/* ── Commit panel ── */
.git-commit-panel{display:none;border-top:1px solid var(--bd);padding:6px 0;margin-top:4px}
.git-commit-panel.open{display:block}
.git-commit-input{
  width:100%;background:var(--bg2);color:var(--t1);border:1px solid var(--bd);
  border-radius:var(--radius-sm);padding:4px 7px;font-size:10px;outline:none;
  resize:none;min-height:38px;font-family:inherit;line-height:1.5;margin-bottom:4px;
}
.git-commit-input:focus{border-color:var(--green)}
.git-commit-input::placeholder{color:var(--t3)}
.git-commit-btns{display:flex;gap:4px}
.git-commit-btns button{
  flex:1;padding:4px 0;border-radius:var(--radius-sm);font-size:10px;font-weight:600;cursor:pointer;transition:all .15s;
}
#git-btn-do-commit{border:1px solid rgba(63,185,80,.4);color:var(--green);background:var(--green-bg)}
#git-btn-commit-push{border:1px solid rgba(88,166,255,.4);color:var(--blue);background:var(--blue-bg)}
.git-status-line{margin-top:3px;font-size:9px;color:var(--t3);min-height:11px}

/* ── Cost bar ── */
.cost-bar{
  display:flex;gap:0;padding:5px 10px;
  background:var(--bg1);border-top:1px solid var(--bd);
  position:fixed;bottom:0;left:0;right:0;
}
.cost-item{flex:1;font-size:9px;color:var(--t3);text-align:center}
.cost-val{color:var(--t2);font-weight:600}

/* ── Toast ── */
#toast{
  position:fixed;bottom:38px;left:50%;transform:translateX(-50%) translateY(4px);
  background:var(--bg2);color:var(--t1);padding:5px 12px;
  border-radius:var(--radius-lg);font-size:10px;font-weight:500;z-index:9999;
  opacity:0;transition:opacity .2s,transform .2s;border:1px solid var(--bd2);
  box-shadow:0 4px 10px rgba(0,0,0,.4);pointer-events:none;white-space:nowrap;
}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.hidden{display:none!important}
.ic{
  display:inline-block;width:13px;height:13px;
  vertical-align:middle;flex-shrink:0;
  fill:none;stroke:currentColor;stroke-width:1.6;
  stroke-linecap:round;stroke-linejoin:round;
}
.ic-fill{ fill:currentColor; stroke:none; }
.ic-sm{ width:11px;height:11px; }
.ic-wb{ width:14px;height:14px; }
</style>
</head>
<body>

<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">
  <symbol id="ic-person" viewBox="0 0 16 16">
    <circle cx="8" cy="5" r="3"/>
    <path d="M2 14c0-3.3 2.7-6 6-6s6 2.7 6 6"/>
  </symbol>
  <symbol id="ic-repo" viewBox="0 0 16 16">
    <rect x="2" y="2" width="12" height="12" rx="2"/>
    <path d="M5 2v12M9 6h3M9 9h3"/>
  </symbol>
  <symbol id="ic-branch" viewBox="0 0 16 16">
    <circle cx="5" cy="4" r="1.5"/>
    <circle cx="5" cy="12" r="1.5"/>
    <circle cx="11" cy="6" r="1.5"/>
    <path d="M5 5.5v5M5 5.5C5 8 11 8 11 7.5"/>
  </symbol>
  <symbol id="ic-server" viewBox="0 0 16 16">
    <rect x="2" y="3" width="12" height="4" rx="1"/>
    <rect x="2" y="9" width="12" height="4" rx="1"/>
    <circle cx="12.5" cy="5" r=".8" class="ic-fill"/>
    <circle cx="12.5" cy="11" r=".8" class="ic-fill"/>
  </symbol>
  <symbol id="ic-zap" viewBox="0 0 16 16">
    <path d="M9 2L4 9h4l-1 5 5-7H8z"/>
  </symbol>
  <symbol id="ic-alert" viewBox="0 0 16 16">
    <path d="M8 2L1 14h14L8 2z"/>
    <path d="M8 7v3M8 11.5v.5"/>
  </symbol>
  <symbol id="ic-github" viewBox="0 0 16 16">
    <path d="M8 1a7 7 0 0 0-2.21 13.63c.35.06.48-.15.48-.34v-1.2c-1.94.42-2.35-.94-2.35-.94-.32-.81-.78-1.03-.78-1.03-.64-.43.05-.42.05-.42.7.05 1.07.72 1.07.72.62 1.07 1.63.76 2.03.58.06-.45.24-.76.44-.93-1.55-.18-3.18-.77-3.18-3.44 0-.76.27-1.38.72-1.87-.07-.18-.31-.88.07-1.84 0 0 .58-.19 1.9.71A6.6 6.6 0 0 1 8 4.8c.59 0 1.18.08 1.73.23 1.32-.9 1.9-.71 1.9-.71.38.96.14 1.66.07 1.84.45.49.72 1.11.72 1.87 0 2.68-1.63 3.26-3.19 3.44.25.22.47.64.47 1.29v1.91c0 .19.13.41.48.34A7 7 0 0 0 8 1z"/>
  </symbol>
  <symbol id="ic-deploy" viewBox="0 0 16 16">
    <path d="M8 2v8M5 7l3-3 3 3"/>
    <path d="M3 12h10"/>
  </symbol>
  <symbol id="ic-layout" viewBox="0 0 16 16">
    <rect x="2" y="2" width="12" height="12" rx="1"/>
    <path d="M2 6h12M6 6v8"/>
  </symbol>
  <symbol id="ic-commit" viewBox="0 0 16 16">
    <circle cx="8" cy="8" r="2.5"/>
    <path d="M2 8h3.5M10.5 8H14"/>
  </symbol>
  <symbol id="ic-push" viewBox="0 0 16 16">
    <path d="M8 11V3M5 6l3-3 3 3"/>
    <path d="M3 13h10"/>
  </symbol>
  <symbol id="ic-link" viewBox="0 0 16 16">
    <path d="M6.5 9.5l3-3"/>
    <path d="M9 4h3v3"/>
    <path d="M7 12H4V4h4"/>
  </symbol>
</svg>
<!-- Ready Bar -->
<div class="ready-bar">
  <div class="rc" id="chip-core" title="Core"><span class="dot"></span>Core</div>
  <div class="rc" id="chip-ai" title="AI"><span class="dot"></span>AI</div>
  <div class="rc" id="chip-docker" title="Docker"><span class="dot"></span>Docker</div>
  <div class="rc" id="chip-github" title="GitHub"><span class="dot"></span>GitHub</div>
</div>

<!-- Current Issue -->
<div class="sec-label">
  <span id="sb-issue-dot" class="issue-dot" style="display:none"></span>현재 이슈
</div>
<div class="mini-card">
  <div class="no-issue" id="sb-no-issue">
    감지된 이슈 없음
  </div>
  <div id="sb-issue-content" class="hidden">
    <div class="issue-summary" id="sb-issue-summary"></div>
    <div class="issue-time" id="sb-issue-time"></div>
  </div>
</div>

<!-- GitHub -->
<div class="sec-label">GitHub</div>
<div class="mini-card" id="sb-github-card">
  <div class="gh-card-row">
    <svg class="ic" style="color:var(--t3)"><use href="#ic-person"/></svg>
    <span class="val gh-user-val" id="sb-gh-user">미연결</span>
  </div>
  <div class="gh-card-row">
    <svg class="ic" style="color:var(--t3)"><use href="#ic-repo"/></svg>
    <span class="val" id="sb-gh-repo">—</span>
  </div>
  <div class="gh-card-row">
    <svg class="ic" style="color:var(--t3)"><use href="#ic-branch"/></svg>
    <span class="val" id="sb-gh-branch">—</span>
  </div>
</div>

<!-- Deploy Status -->
<div class="sec-label"><svg class="ic ic-sm" style="color:var(--t2)"><use href="#ic-server"/></svg> 배포 상태</div>
<div class="mini-card">
  <div class="sb-deploy-row">
    <span style="color:var(--t2);font-size:11px" id="sb-deploy-status">—</span>
    <span class="sb-health-badge" id="sb-deploy-health">—</span>
  </div>
</div>

<!-- Quick Launch -->
<div class="sec-label"><svg class="ic ic-sm" style="color:var(--t2)"><use href="#ic-zap"/></svg> 빠른 실행</div>
<div class="quick-btns">

  <!-- ── 에러 분석 ── -->
  <button class="quick-btn" id="sb-btn-error">
    <svg class="ic" style="color:var(--red)"><use href="#ic-alert"/></svg>
    <span class="q-label">에러 분석</span>
    <span class="q-badge" id="sb-error-badge" style="display:none">1</span>
    <span class="q-arrow">►</span>
  </button>
  <div class="sb-panel" id="sb-panel-error">
    <div class="sb-panel-section">
      <div class="sb-panel-label">오류 내용 (선택 또는 붙여넣기)</div>
      <textarea class="sb-panel-textarea" id="sb-error-input" rows="3"
        placeholder="터미널 출력, 메시지 붙여넣기...
경고: AI가 현재 파일을 자동으로 검색합니다"></textarea>
    </div>
    <button class="sb-panel-btn primary" id="sb-do-analyze">AI 분석 실행</button>
    <div class="sb-result" id="sb-analyze-result">
      <div class="sb-result-title" id="sb-ar-title"></div>
      <div id="sb-ar-body"></div>
    </div>
    <div class="sb-panel-row" id="sb-patch-btns" style="display:none;margin-top:5px">
      <button class="sb-panel-btn success" id="sb-do-approve">패치 적용</button>
      <button class="sb-panel-btn danger" id="sb-do-reject">거절</button>
    </div>
    <div class="sb-panel-status" id="sb-analyze-status"></div>
    <div class="sb-panel-footer">
      <button class="sb-wb-link" data-wb-page="error"><svg class="ic ic-sm"><use href="#ic-link"/></svg> Workbench에서 열기</button>
    </div>
  </div>

  <!-- ── GitHub Hub ── -->
  <button class="quick-btn" id="sb-btn-github">
    <svg class="ic" style="color:var(--blue)"><use href="#ic-github"/></svg>
    <span class="q-label">GitHub Hub</span>
    <span class="q-arrow">►</span>
  </button>
  <div class="sb-panel" id="sb-panel-github">
    <div class="sb-panel-section">
      <div class="sb-panel-label">연결 상태</div>
      <div id="sb-gh-panel-status" style="font-size:10px;color:var(--t2)">로딩 중...</div>
    </div>
    <!-- 미인증 시 GitHub 연결 버튼 -->
    <div id="sb-gh-connect-section">
      <button class="sb-panel-btn primary" id="sb-gh-connect" style="margin-top:6px;display:flex;align-items:center;justify-content:center;gap:5px">
        <svg class="ic ic-sm"><use href="#ic-github"/></svg> GitHub 연결
      </button>
      <div class="sb-panel-status" id="sb-gh-connect-status" style="text-align:center;margin-top:3px"></div>
    </div>
    <!-- 인증 완료 시 빠른 커밋 섹션 -->
    <div id="sb-gh-authed-section" style="display:none">
      <div class="sb-panel-divider"></div>
      <div class="sb-panel-section">
        <div class="sb-panel-label">빠른 커밋</div>
        <textarea class="sb-panel-textarea" id="sb-gh-commit-msg" rows="2"
          placeholder="커밋 메시지..."></textarea>
        <div class="sb-panel-row">
          <button class="sb-panel-btn success" id="sb-gh-do-commit"><svg class="ic ic-sm"><use href="#ic-commit"/></svg> Commit</button>
          <button class="sb-panel-btn primary" id="sb-gh-do-push">↑ Push</button>
          <button class="sb-panel-btn primary" id="sb-gh-do-commit-push"><svg class="ic ic-sm"><use href="#ic-commit"/></svg> Commit + Push</button>
        </div>
      </div>
    </div>
    <div class="sb-panel-status" id="sb-gh-panel-status-line"></div>
    <div class="sb-panel-footer">
      <button class="sb-wb-link" data-wb-page="github"><svg class="ic ic-sm"><use href="#ic-link"/></svg> Workbench에서 열기</button>
    </div>
  </div>

  <!-- ── 배포 센터 ── -->
  <button class="quick-btn" id="sb-btn-deploy">
    <svg class="ic" style="color:var(--blue)"><use href="#ic-deploy"/></svg>
    <span class="q-label">배포 센터</span>
    <span class="q-arrow">►</span>
  </button>
  <div class="sb-panel" id="sb-panel-deploy">
    <div class="sb-panel-section">
      <div class="sb-panel-label">현재 상태</div>
      <div class="sb-dep-row">
        <span class="key">스테이지</span>
        <span class="val" id="sb-dep-stage">—</span>
      </div>
      <div class="sb-dep-row">
        <span class="key">Health</span>
        <span class="sb-dep-badge" id="sb-dep-health">—</span>
      </div>
    </div>
    <div class="sb-panel-divider"></div>
    <div class="sb-panel-section">
      <div class="sb-panel-label">로컬 Docker 배포</div>
      <button class="sb-panel-btn" id="sb-dep-dockerfile">Dockerfile 생성</button>
      <button class="sb-panel-btn primary" id="sb-dep-build">Docker 빌드</button>
      <button class="sb-panel-btn success" id="sb-dep-start">배포 시작</button>
      <button class="sb-panel-btn danger" id="sb-dep-rollback">롤백</button>
    </div>
    <div class="sb-panel-divider"></div>
    <div class="sb-panel-section">
      <div class="sb-panel-label" style="display:flex;align-items:center;gap:4px">
        AWS EC2 배포
        <span class="git-chip" id="sb-ec2-ready-chip" style="font-size:9px;padding:1px 5px;background:var(--yellow-bg);color:var(--yellow)">확인 중</span>
      </div>
      <div id="sb-ec2-issues" style="font-size:10px;color:var(--red);margin-bottom:4px;display:none"></div>
      <div class="sb-dep-row" style="margin-bottom:4px">
        <span class="key">이미지명</span>
        <input class="gd-new-input" id="sb-ec2-image" placeholder="recoder-app" style="width:120px;font-size:10px;padding:2px 5px">
      </div>
      <div class="sb-dep-row" style="margin-bottom:4px">
        <span class="key">ECR 레포</span>
        <input class="gd-new-input" id="sb-ec2-repo" placeholder="recoder-app" style="width:120px;font-size:10px;padding:2px 5px">
      </div>
      <div class="sb-dep-row" style="margin-bottom:6px">
        <span class="key">태그</span>
        <input class="gd-new-input" id="sb-ec2-tag" placeholder="latest" style="width:120px;font-size:10px;padding:2px 5px">
      </div>
      <button class="sb-panel-btn primary" id="sb-ec2-deploy-btn">🚀 EC2 배포</button>
      <div id="sb-ec2-progress" style="display:none;margin-top:6px">
        <div class="sb-dep-row">
          <span class="key">단계</span>
          <span class="val" id="sb-ec2-stage">—</span>
        </div>
        <div id="sb-ec2-log" style="font-size:10px;color:var(--t2);max-height:80px;overflow-y:auto;margin-top:4px;white-space:pre-wrap;word-break:break-all;background:var(--bg2);padding:4px;border-radius:4px"></div>
      </div>
    </div>
    <div class="sb-result" id="sb-dep-result">
      <div class="sb-result-title" id="sb-dep-res-title"></div>
      <div id="sb-dep-res-body"></div>
    </div>
    <div class="sb-panel-status" id="sb-dep-status-line"></div>
    <div class="sb-panel-footer">
      <button class="sb-wb-link" data-wb-page="deploy"><svg class="ic ic-sm"><use href="#ic-link"/></svg> Workbench에서 열기</button>
    </div>
  </div>

</div>

<!-- Workbench open -->
<button class="wb-open-btn" id="btn-open-workbench">
  <svg class="ic ic-wb"><use href="#ic-layout"/></svg> Workbench 열기
</button>

<!-- Git Strip -->
<div class="git-strip" id="git-panel">
  <div class="git-row1">
    <div class="git-acct" id="git-account">
      <svg class="ic ic-sm"><use href="#ic-person"/></svg>
      <span id="git-account-name">—</span>
    </div>
    <button class="git-branch-pill" id="git-branch-btn">
      <svg class="ic ic-sm"><use href="#ic-branch"/></svg>
      <span class="bname" id="git-branch-name">로딩 중...</span>
      <span style="color:var(--t3);font-size:9px;flex-shrink:0">▼</span>
    </button>
    <span class="git-chip git-chip-changed hidden" id="git-uncommitted-badge">0 변경</span>
    <span class="git-chip git-chip-ahead hidden" id="git-ahead-badge">0 Commit 업</span>
  </div>
  <div class="git-row2">
    <button id="git-btn-commit"><svg class="ic ic-sm"><use href="#ic-commit"/></svg> Commit</button>
    <button id="git-btn-push"><svg class="ic ic-sm"><use href="#ic-push"/></svg> Push</button>
  </div>
  <!-- Branch dropdown -->
  <div class="git-dropdown" id="git-dropdown">
    <div class="gd-section">로컬 브랜치</div>
    <div id="git-local-branches"><div class="gd-branch" style="color:var(--t3)">로딩 중...</div></div>
    <div class="gd-section" style="margin-top:4px">원격 브랜치</div>
    <div id="git-remote-branches"><div class="gd-branch" style="color:var(--t3)">로딩 중...</div></div>
    <div class="gd-new-row">
      <input class="gd-new-input" id="git-new-branch-input" placeholder="새 브랜치 이름" type="text">
      <button class="gd-new-btn" id="git-btn-branch-create">+ 생성</button>
    </div>
  </div>
  <!-- Commit panel -->
  <div class="git-commit-panel" id="git-commit-panel">
    <textarea class="git-commit-input" id="git-commit-msg" placeholder="커밋 메시지..." rows="2"></textarea>
    <div class="git-commit-btns">
      <button id="git-btn-do-commit"><svg class="ic ic-sm"><use href="#ic-commit"/></svg> Commit</button>
      <button id="git-btn-commit-push"><svg class="ic ic-sm"><use href="#ic-commit"/></svg> Commit + Push</button>
    </div>
    <div class="git-status-line" id="git-commit-status"></div>
  </div>
</div>

<!-- Cost bar -->
<div class="cost-bar">
  <div class="cost-item">Today <span class="cost-val" id="cost-daily">-</span></div>
  <div class="cost-item">Month <span class="cost-val" id="cost-monthly">-</span></div>
  <div class="cost-item">Calls <span class="cost-val" id="cost-calls">-</span></div>
</div>

<div id="toast"></div>

<script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
}
