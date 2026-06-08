/**
 * ReCoder Workbench Panel
 *
 * 별도 VSCode WebviewPanel 로 열리는 대시보드.
 * 사이드바와는 다른 큰 화면 (탭 + 최근 활동 + 로그 + 빠른 작업).
 *
 * 사용:
 *   await vscode.commands.executeCommand('recoder.openWorkbench');
 *
 * 메시지 프로토콜:
 *   webview → extension:  { type, payload? }
 *     - 'wb.ready'            웹뷰 준비 완료, 초기 상태 요청
 *     - 'wb.analyze'          새 에러 분석 시작
 *     - 'wb.openSidebar'      사이드바 포커스
 *     - 'wb.poll.health'      Core 헬스 + 비용 즉시 갱신 요청
 *     - 'wb.tab'              { tab: 'command'|'error'|'github'|'deploy' }
 *   extension → webview:
 *     - 'wb.healthUpdate'    { ...CoreHealth }
 *     - 'wb.costUpdate'      { ...CostSummary }
 *     - 'wb.activity'        { items: ActivityItem[] }
 *     - 'wb.log'             { pane: 'ai'|'docker'|'github'|'deploy'|'health', line: string }
 */
import * as vscode from 'vscode';
import { CoreManager } from '../core/CoreManager';
import { ApiClient } from '../core/ApiClient';
import { PollingService } from '../core/PollingService';
import { renderWorkbenchHtml } from './workbenchHtml';

export class WorkbenchPanel {
    public static readonly viewType = 'recoder.workbench';
    private static _current: WorkbenchPanel | undefined;

    private readonly _panel: vscode.WebviewPanel;
    private _activity: { dot: string; text: string; time: string }[] = [];
    private _pollTimer: ReturnType<typeof setInterval> | null = null;
    private _diagnosticsInflight: boolean = false;
    private _diagnosticsLoaded: boolean = false;

    // ── Workbench bidirectional sync (Discord ↔ Core ↔ VSCode) ──────────
    /** /workbench/events cursor — index offset into Core's in-memory event buffer
     *  (NOT a timestamp). Each /events response carries next_offset which we use
     *  for the next call. Starts at 0 = "give me everything from the beginning". */
    private _workbenchEventsCursor: number = 0;
    private _workbenchPollTimer: ReturnType<typeof setInterval> | null = null;
    /** Current workbench mode (cached, last seen from Core). */
    private _workbenchMode: 'home' | 'build' | 'ship' | 'operate' | 'recover' = 'home';

    static createOrShow(
        extensionUri: vscode.Uri,
        apiClient: ApiClient,
        coreManager: CoreManager,
        polling: PollingService,
    ): WorkbenchPanel {
        const column = vscode.window.activeTextEditor?.viewColumn ?? vscode.ViewColumn.One;
        if (WorkbenchPanel._current) {
            WorkbenchPanel._current._panel.reveal(column);
            return WorkbenchPanel._current;
        }

        const panel = vscode.window.createWebviewPanel(
            WorkbenchPanel.viewType,
            'ReCoder Workbench',
            column,
            {
                enableScripts: true,
                retainContextWhenHidden: true,
                localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
            }
        );

        WorkbenchPanel._current = new WorkbenchPanel(panel, extensionUri, apiClient, coreManager, polling);
        return WorkbenchPanel._current;
    }

    private constructor(
        panel: vscode.WebviewPanel,
        private readonly _extensionUri: vscode.Uri,
        private readonly _apiClient: ApiClient,
        private readonly _coreManager: CoreManager,
        private readonly _polling: PollingService,
    ) {
        this._panel = panel;
        this._panel.webview.html = this._renderHtml(panel.webview);

        this._panel.onDidDispose(() => this._dispose(), null, []);
        this._panel.webview.onDidReceiveMessage(async (msg: { type: string; payload?: any }) => {
            await this._handleMessage(msg);
        });

        // 즉시 health/cost/diagnostics 푸시 (캐시된 값 사용 → chip 색상 즉시 표시)
        // 이전 800ms setTimeout → 사용자가 panel 열고 1초 동안 빈 chip 봤음. 제거.
        void this._pushHealthAndCost();
        this._startPolling();

        // Workbench 양방향 sync — 즉시 시작
        void this._pushWorkbenchState();
        this._startWorkbenchPolling();
    }

    public addActivity(dotClass: 'ok' | 'warn' | 'fail' | 'info', text: string): void {
        const item = { dot: dotClass, text, time: this._now() };
        this._activity.unshift(item);
        if (this._activity.length > 30) this._activity.pop();
        this._panel.webview.postMessage({ type: 'wb.activity', payload: { items: this._activity } });
    }

    public pushLog(pane: 'ai' | 'docker' | 'github' | 'deploy' | 'health', line: string): void {
        this._panel.webview.postMessage({ type: 'wb.log', payload: { pane, line } });
    }

    public pushDiagnostics(diag: import('../types').DiagnosticsResult): void {
        this._panel.webview.postMessage({ type: 'wb.diagnosticsUpdate', payload: diag });
    }

    // ──────────────────────────────────────────────────────────────────

    private async _handleMessage(msg: { type: string; payload?: any }): Promise<void> {
        switch (msg.type) {
            case 'wb.ready':
                await this._pushHealthAndCost();
                this._panel.webview.postMessage({ type: 'wb.activity', payload: { items: this._activity } });
                break;
            case 'wb.analyze':
                await vscode.commands.executeCommand('recoder.analyzeError');
                this.addActivity('info', '에러 분석 시작');
                break;
            case 'wb.openSidebar':
                await vscode.commands.executeCommand('recoder.sidebarView.focus');
                break;
            case 'wb.poll.health':
                await this._pushHealthAndCost();
                break;
            case 'wb.tab':
                // 클라이언트 측에서 탭 전환만 — 별도 처리 불필요
                break;
            case 'wb.generateDockerfile':
                await vscode.commands.executeCommand('recoder.generateDockerfile');
                this.addActivity('info', 'Dockerfile 생성 요청');
                break;
            case 'wb.generateGithubActions':
                try {
                    await vscode.commands.executeCommand('recoder.generateGithubActions');
                    this.addActivity('info', 'GitHub Actions 워크플로우 생성 요청');
                } catch (err) {
                    this.addActivity('fail', `GitHub Actions 생성 실패: ${err}`);
                }
                break;
            case 'wb.runDiagnostics':
                await vscode.commands.executeCommand('recoder.runDiagnostics');
                this.addActivity('info', '진단 재실행');
                break;
            case 'wb.restartCore':
                await vscode.commands.executeCommand('recoder.restartCore');
                this.addActivity('warn', 'Core 재시작');
                break;
            // ── Workbench bidirectional sync (VSCode → Core → Discord) ───
            case 'wb.changeMode': {
                const mode = (msg.payload?.mode as string) || 'build';
                try {
                    await this._apiClient.workbenchChangeMode(
                        mode as 'build' | 'ship' | 'operate' | 'recover',
                        'vscode',
                    );
                    // 즉시 자기 자신도 갱신 (Core 응답 후 다음 polling tick 까지 안 기다림)
                    void this._pollWorkbenchEvents();
                } catch (err) {
                    this.addActivity('fail', `모드 전환 실패: ${err}`);
                }
                break;
            }

            // ── GitHub Hub ──────────────────────────────────────────────
            case 'wb.gh.login':
                try {
                    await vscode.commands.executeCommand('recoder.githubLogin');
                    this.addActivity('info', 'GitHub 로그인 시도');
                    // 로그인 후 상태 갱신을 webview 에 보냄
                    void this._pushGithubStatus();
                } catch (err) {
                    this.addActivity('fail', `GitHub 로그인 실패: ${err}`);
                }
                break;
            case 'wb.gh.logout':
                try {
                    await vscode.commands.executeCommand('recoder.githubLogout');
                    this.addActivity('info', 'GitHub 로그아웃');
                    void this._pushGithubStatus();
                } catch (err) {
                    this.addActivity('fail', `GitHub 로그아웃 실패: ${err}`);
                }
                break;
            case 'wb.gh.status':
                await this._pushGithubStatus();
                break;
            case 'wb.gh.listRepos':
                try {
                    const result = await this._apiClient.listGithubRepos();
                    this._panel.webview.postMessage({
                        type: 'wb.gh.reposResult',
                        payload: { repos: result.repos ?? [] },
                    });
                    this.addActivity('info', `GitHub 레포 ${(result.repos ?? []).length}개 로드`);
                } catch (err) {
                    this.pushLog('github', `[ERR] 레포 목록 조회 실패: ${err}`);
                    this.addActivity('fail', `GitHub 레포 조회 실패: ${err}`);
                }
                break;
            case 'wb.gh.createRepo': {
                const p = msg.payload ?? {};
                const wsPath = (p.workspace_path as string) || this._getWorkspacePath();
                if (!wsPath) {
                    this._panel.webview.postMessage({ type: 'wb.gh.createRepoResult', payload: { ok: false, error: '워크스페이스가 열려 있지 않음' } });
                    break;
                }
                if (!p.name) {
                    this._panel.webview.postMessage({ type: 'wb.gh.createRepoResult', payload: { ok: false, error: '레포 이름이 비어 있음' } });
                    break;
                }
                try {
                    const result = await this._apiClient.githubCreateRepo({
                        workspace_path: wsPath,
                        name: String(p.name),
                        private: !!p.private,
                        description: (p.description as string) || '',
                    });
                    const url = result.html_url ?? '';
                    this._panel.webview.postMessage({
                        type: 'wb.gh.createRepoResult',
                        payload: { ok: true, url, message: `레포 생성 완료: ${url || result.status}` },
                    });
                    this.addActivity('ok', `레포 생성 완료: ${p.name}`);
                } catch (err) {
                    this._panel.webview.postMessage({ type: 'wb.gh.createRepoResult', payload: { ok: false, error: String(err) } });
                    this.addActivity('fail', `레포 생성 실패: ${err}`);
                }
                break;
            }
            case 'wb.gh.setSecret': {
                const p = msg.payload ?? {};
                if (!p.repo || !p.name || p.value === undefined) {
                    this._panel.webview.postMessage({ type: 'wb.gh.secretResult', payload: { ok: false, error: 'repo / name / value 모두 필요' } });
                    break;
                }
                try {
                    const r = await this._apiClient.githubSetSecret({
                        repo: String(p.repo),
                        name: String(p.name),
                        value: String(p.value),
                    });
                    this._panel.webview.postMessage({
                        type: 'wb.gh.secretResult',
                        payload: { ok: true, name: p.name, message: r.message ?? `Secret 등록: ${p.repo}/${p.name}` },
                    });
                    this.addActivity('ok', `Secret 등록: ${p.repo}/${p.name}`);
                } catch (err) {
                    this._panel.webview.postMessage({ type: 'wb.gh.secretResult', payload: { ok: false, error: String(err) } });
                    this.addActivity('fail', `Secret 등록 실패: ${err}`);
                }
                break;
            }
            case 'wb.gh.push': {
                const p = msg.payload ?? {};
                const wsPath = (p.workspace_path as string) || this._getWorkspacePath();
                if (!wsPath) {
                    this._panel.webview.postMessage({ type: 'wb.gh.pushResult', payload: { ok: false, error: '워크스페이스가 열려 있지 않음' } });
                    break;
                }
                try {
                    const r = await this._apiClient.gitPush({
                        workspace_path: wsPath,
                        branch: (p.branch as string) || '',
                        force: !!p.force,
                    });
                    this._panel.webview.postMessage({
                        type: 'wb.gh.pushResult',
                        payload: { ok: true, branch: r.branch, message: r.message ?? `push 완료 (branch=${r.branch ?? '?'})` },
                    });
                    this.addActivity('ok', `git push 완료`);
                } catch (err) {
                    this._panel.webview.postMessage({ type: 'wb.gh.pushResult', payload: { ok: false, error: String(err) } });
                    this.addActivity('fail', `git push 실패: ${err}`);
                }
                break;
            }
            case 'wb.gh.listRuns': {
                const repo = String(msg.payload?.repo ?? '');
                if (!repo) {
                    this.pushLog('github', '[ERR] repo 인자 누락');
                    break;
                }
                try {
                    const r = await this._apiClient.githubListRuns(repo);
                    this._panel.webview.postMessage({
                        type: 'wb.gh.runsResult',
                        payload: { repo, runs: r.workflow_runs ?? [] },
                    });
                    this.pushLog('github', `[OK] ${repo}: ${(r.workflow_runs ?? []).length}개 실행`);
                } catch (err) {
                    this.pushLog('github', `[ERR] runs 조회 실패: ${err}`);
                }
                break;
            }

            // ── Deploy Center 사전 점검 (자동 발사) ────────────────────
            case 'wb.deploy.precheck': {
                const items = await this._collectDeployPrechecks();
                this._panel.webview.postMessage({
                    type: 'wb.deploy.precheckResult',
                    payload: { items },
                });
                break;
            }
            // 일반 VS Code command 트리거 (precheck "해결" 버튼용)
            case 'wb.cmd': {
                const cmd = String(msg.payload?.cmd ?? '');
                if (cmd && cmd.startsWith('recoder.')) {
                    try { await vscode.commands.executeCommand(cmd); } catch (err) {
                        this.addActivity('fail', `${cmd} 실패: ${err}`);
                    }
                }
                break;
            }

            // ── Deploy Center ───────────────────────────────────────────
            case 'wb.deploy.localDocker':
                // 호환용 (구버전 button id) — 새 wizard 로 위임
                this._panel.webview.postMessage({
                    type: 'wb.local.generateResult',
                    payload: { ok: false, error: '새 wizard 의 1. Dockerfile 생성 버튼을 사용하세요' },
                });
                break;

            // ── Local Docker 6단계 wizard (워크벤치 직접 실행) ─────────
            case 'wb.local.generate': {
                const ws = this._getWorkspacePath();
                if (!ws) {
                    this._panel.webview.postMessage({ type: 'wb.local.generateResult', payload: { ok: false, error: '워크스페이스 없음' } });
                    break;
                }
                try {
                    const proposal = await this._apiClient.generateDockerfile(ws);
                    const content = (proposal as unknown as { content?: string }).content ?? '';
                    const proposalId = (proposal as unknown as { proposal_id?: string }).proposal_id ?? '';
                    this._panel.webview.postMessage({
                        type: 'wb.local.generateResult',
                        payload: { ok: true, proposal_id: proposalId, content },
                    });
                    this.addActivity('ok', 'Dockerfile 생성 완료');
                } catch (err) {
                    this._panel.webview.postMessage({ type: 'wb.local.generateResult', payload: { ok: false, error: String(err) } });
                    this.addActivity('fail', `Dockerfile 생성 실패: ${err}`);
                }
                break;
            }
            case 'wb.local.approve': {
                const proposalId = String(msg.payload?.proposal_id ?? '');
                if (!proposalId) {
                    this._panel.webview.postMessage({ type: 'wb.local.approveResult', payload: { ok: false, error: 'proposal_id 누락' } });
                    break;
                }
                try {
                    await this._apiClient.approveDockerfile(proposalId, true);
                    this._panel.webview.postMessage({
                        type: 'wb.local.approveResult',
                        payload: { ok: true, path: 'Dockerfile' },
                    });
                    this.addActivity('ok', 'Dockerfile 승인 + 저장');
                } catch (err) {
                    this._panel.webview.postMessage({ type: 'wb.local.approveResult', payload: { ok: false, error: String(err) } });
                }
                break;
            }
            case 'wb.local.scan': {
                const ws = this._getWorkspacePath();
                if (!ws) {
                    this._panel.webview.postMessage({ type: 'wb.local.scanResult', payload: { ok: false, error: '워크스페이스 없음' } });
                    break;
                }
                try {
                    // 보안 스캔: gitleaks 로 하드코딩된 시크릿 탐지 (빌드 전에 코드 정적 스캔).
                    const r = await this._apiClient.runScan('gitleaks', ws);
                    this._panel.webview.postMessage({
                        type: 'wb.local.scanResult',
                        payload: { ok: true, result: r },
                    });
                    this.addActivity('ok', '보안 스캔(gitleaks) 완료');
                } catch (err) {
                    this._panel.webview.postMessage({ type: 'wb.local.scanResult', payload: { ok: false, error: String(err) } });
                }
                break;
            }
            case 'wb.local.deploy': {
                const ws = this._getWorkspacePath();
                if (!ws) {
                    this._panel.webview.postMessage({ type: 'wb.local.deployProgress', payload: { stage: 'failed', error: '워크스페이스 없음', finished: true, line: '워크스페이스가 열려있지 않습니다.' } });
                    break;
                }
                const p = msg.payload ?? {};
                try {
                    this._panel.webview.postMessage({ type: 'wb.local.deployProgress', payload: { stage: 'build', line: '[…] 배포 플랜 생성 중' } });
                    // DeployMethod LOCAL_DOCKER 는 enum 문자열로 보냄
                    const plan = await this._apiClient.createDeploymentPlan(
                        ws,
                        'local_docker' as unknown as import('../types').DeployMethod,
                        undefined,
                        String(p.image || 'recoder-app'),
                        String(p.image || 'recoder-app'),
                        Number(p.host_port || 8000),
                        Number(p.container_port || 8000),
                    );
                    const planId = (plan as unknown as { plan_id?: string }).plan_id || '';
                    this._panel.webview.postMessage({ type: 'wb.local.deployProgress', payload: { stage: 'build', line: '[OK] 플랜 생성됨 — 실행 시작' } });
                    const result = await this._apiClient.executeDeployment(planId, true);
                    if (result.status === 'ok' || result.deployment_id) {
                        this._panel.webview.postMessage({
                            type: 'wb.local.deployProgress',
                            payload: { stage: 'health', finished: true, line: `[OK] 배포 완료 (id=${result.deployment_id ?? '?'})` },
                        });
                        this.addActivity('ok', 'Local Docker 배포 완료');
                    } else {
                        // 컨테이너가 시작/헬스에 실패한 경우 — stderr 에서 핵심 사유 한 줄 추출
                        const errText = (result.stderr || result.stdout || '').trim();
                        const lines = errText.split('\n').map(s => s.trim()).filter(Boolean);
                        const summary = lines.reverse().find(l => /error|exception|traceback|keyerror|exited|not running|unhealthy|refused/i.test(l))
                            || lines[0] || '컨테이너가 시작되지 못했습니다.';
                        this._panel.webview.postMessage({
                            type: 'wb.local.deployProgress',
                            payload: { stage: 'run', error: 'execute 실패', error_summary: summary, finished: true, line: `[FAIL] ${result.status} — ${summary}` },
                        });
                    }
                } catch (err) {
                    this._panel.webview.postMessage({
                        type: 'wb.local.deployProgress',
                        payload: { stage: 'build', error: String(err), finished: true, line: `[ERR] ${err}` },
                    });
                    this.addActivity('fail', `Local Docker 배포 실패: ${err}`);
                }
                break;
            }

            // ── GitHub Actions wizard (워크벤치 직접) ─────────────────
            case 'wb.actions.generate': {
                const ws = this._getWorkspacePath();
                if (!ws) {
                    this._panel.webview.postMessage({ type: 'wb.actions.generateResult', payload: { ok: false, error: '워크스페이스 없음' } });
                    break;
                }
                try {
                    const proposal = await this._apiClient.generateGithubActions(ws);
                    const content = (proposal as unknown as { content?: string }).content ?? '';
                    const proposalId = (proposal as unknown as { proposal_id?: string }).proposal_id ?? '';
                    this._panel.webview.postMessage({
                        type: 'wb.actions.generateResult',
                        payload: { ok: true, proposal_id: proposalId, content },
                    });
                    this.addActivity('ok', 'GitHub Actions 워크플로 생성');
                } catch (err) {
                    this._panel.webview.postMessage({ type: 'wb.actions.generateResult', payload: { ok: false, error: String(err) } });
                    this.addActivity('fail', `워크플로 생성 실패: ${err}`);
                }
                break;
            }
            case 'wb.actions.approve': {
                const proposalId = String(msg.payload?.proposal_id ?? '');
                if (!proposalId) {
                    this._panel.webview.postMessage({ type: 'wb.actions.approveResult', payload: { ok: false, error: 'proposal_id 누락' } });
                    break;
                }
                try {
                    await this._apiClient.approveGithubActions(proposalId, true);
                    this._panel.webview.postMessage({
                        type: 'wb.actions.approveResult',
                        payload: { ok: true, path: '.github/workflows/ci-cd.yml' },
                    });
                    this.addActivity('ok', '워크플로 저장');
                } catch (err) {
                    this._panel.webview.postMessage({ type: 'wb.actions.approveResult', payload: { ok: false, error: String(err) } });
                }
                break;
            }
            case 'wb.deploy.ec2': {
                const p = msg.payload ?? {};
                const wsPath = (p.workspace_path as string) || this._getWorkspacePath();
                this.pushLog('deploy', `[…] EC2 배포 시작 (host=${p.ec2_host || 'env'})`);
                try {
                    const ready = await this._apiClient.ec2DeployReady();
                    if (!ready.ready) {
                        this.pushLog('deploy', `[BLOCKED] ${ready.issues.join('; ')}`);
                        this.addActivity('fail', 'EC2 사전 점검 실패');
                        break;
                    }
                    const r = await this._apiClient.deployEc2({
                        workspace_path: wsPath,
                        image_name: (p.image_name as string) || 'recoder-app',
                        repo_name: (p.repo_name as string) || 'recoder-app',
                        tag: (p.tag as string) || 'latest',
                        container_name: (p.container_name as string) || 'recoder-app',
                        host_port: Number(p.host_port ?? 8000),
                        container_port: Number(p.container_port ?? 8000),
                        health_check_path: (p.health_check_path as string) || '/health',
                        ecr_registry: (p.ecr_registry as string) || '',
                        ec2_host: (p.ec2_host as string) || '',
                        ec2_ssh_key: (p.ec2_ssh_key as string) || '',
                        aws_region: (p.aws_region as string) || '',
                        ec2_user: (p.ec2_user as string) || 'ec2-user',
                    });
                    this.pushLog('deploy', `[OK] ${r.message}`);
                    this.addActivity('info', 'EC2 배포 시작 (백그라운드)');
                    this._startEc2StatusPolling();
                } catch (err) {
                    this.pushLog('deploy', `[ERR] ${err}`);
                    this.addActivity('fail', `EC2 배포 실패: ${err}`);
                }
                break;
            }
            case 'wb.deploy.ecs': {
                const p = msg.payload ?? {};
                const wsPath = (p.workspace_path as string) || this._getWorkspacePath();
                this.pushLog('deploy', `[…] ECS Fargate 배포 시작`);
                try {
                    const ready = await this._apiClient.ecsDeployReady();
                    if (!ready.ready) {
                        this.pushLog('deploy', `[BLOCKED] ${ready.issues.join('; ')}`);
                        this.addActivity('fail', 'ECS 사전 점검 실패');
                        break;
                    }
                    const r = await this._apiClient.deployEcs({
                        workspace_path: wsPath,
                        image_name: (p.image_name as string) || 'recoder-app',
                        repo_name: (p.repo_name as string) || 'recoder-app',
                        tag: (p.tag as string) || 'latest',
                        ecr_registry: (p.ecr_registry as string) || '',
                        ecs_cluster: (p.ecs_cluster as string) || '',
                        ecs_service: (p.ecs_service as string) || '',
                        aws_region: (p.aws_region as string) || '',
                        container_port: Number(p.container_port ?? 8000),
                        cpu: (p.cpu as string) || '256',
                        memory: (p.memory as string) || '512',
                        task_family: (p.task_family as string) || 'recoder-task',
                        environment: (p.environment as string) || 'staging',
                        branch: (p.branch as string) || '',
                        skip_sbom: !!p.skip_sbom,
                        skip_opa: !!p.skip_opa,
                    });
                    this.pushLog('deploy', `[OK] ${r.message}`);
                    this.addActivity('info', 'ECS 배포 시작 (백그라운드)');
                    this._startEcsStatusPolling();
                } catch (err) {
                    this.pushLog('deploy', `[ERR] ${err}`);
                    this.addActivity('fail', `ECS 배포 실패: ${err}`);
                }
                break;
            }
            case 'wb.deploy.ec2.status':
                try {
                    const s = await this._apiClient.getEc2DeployStatus();
                    this._panel.webview.postMessage({ type: 'wb.deploy.ec2.statusResult', payload: s });
                } catch { /* ignore */ }
                break;
            case 'wb.deploy.ecs.status':
                try {
                    const s = await this._apiClient.getEcsDeployStatus();
                    this._panel.webview.postMessage({ type: 'wb.deploy.ecs.statusResult', payload: s });
                } catch { /* ignore */ }
                break;

            // ── Discord Bridge 설정 (Make 채널 / 봇 초대 / 길드 선택) ───────
            case 'wb.discord.fetchStatus':
                await this._pushDiscordStatus();
                break;
            case 'wb.discord.fetchInviteUrl':
                await this._pushDiscordInviteUrl();
                break;
            case 'wb.discord.fetchGuilds':
                await this._pushDiscordGuilds();
                break;
            case 'wb.discord.fetchChannels': {
                const gid = String(msg.payload?.guild_id ?? '').trim();
                if (!gid) {
                    this._panel.webview.postMessage({
                        type: 'wb.discord.error',
                        payload: { context: 'fetchChannels', message: 'guild_id 누락' },
                    });
                    break;
                }
                await this._pushDiscordChannels(gid);
                break;
            }
            case 'wb.discord.setChannel': {
                const channelId = String(msg.payload?.channel_id ?? '').trim();
                try {
                    const r = await this._botHttpFetch(
                        '/api/v1/bridge/channel',
                        { method: 'PUT', body: JSON.stringify({ channel_id: channelId }) },
                    );
                    this._panel.webview.postMessage({
                        type: 'wb.discord.setChannelResult',
                        payload: { ok: true, ...r },
                    });
                    this.addActivity('ok', channelId
                        ? `Discord Make 채널 저장: ${r.channel_name ?? channelId}`
                        : 'Discord Make 채널 해제');
                    // 저장 후 상태도 재푸시
                    await this._pushDiscordStatus();
                } catch (err) {
                    this._panel.webview.postMessage({
                        type: 'wb.discord.setChannelResult',
                        payload: { ok: false, error: String(err) },
                    });
                    this.addActivity('fail', `Discord 채널 저장 실패: ${err}`);
                }
                break;
            }
            case 'wb.discord.openInvite': {
                try {
                    // 1) 설정된 client_id 로 로컬 생성(봇 실행 불필요) → 2) 없으면 봇 API 폴백
                    let url = this._buildLocalInviteUrl();
                    if (!url) {
                        const r = await this._botHttpFetch('/api/v1/bridge/invite-url');
                        url = (r?.invite_url as string | undefined) || '';
                    }
                    if (url) {
                        await vscode.env.openExternal(vscode.Uri.parse(url));
                        this.addActivity('info', '봇 초대 링크 열기');
                    } else {
                        this.addActivity('fail', '초대 URL 없음 — 설정 recoder.discord.clientId 를 넣거나 봇을 켜세요');
                    }
                } catch (err) {
                    this.addActivity('fail', `봇 초대 실패: ${err}`);
                }
                break;
            }

            default:
                console.warn('[WorkbenchPanel] Unknown message:', msg.type);
        }
    }

    // ─────────────── Discord Bridge HTTP helpers ──────────────────────

    /** 봇 HTTP 서버(127.0.0.1:8765 기본) 로 fetch.
     *  recoder.bridge.httpPort / recoder.bridge.host / recoder.bridge.registrationKey 설정 사용.
     */
    private async _botHttpFetch(path: string, init?: { method?: string; body?: string }): Promise<any> {
        const cfg = vscode.workspace.getConfiguration('recoder.bridge');
        const host = (cfg.get<string>('host') || '127.0.0.1').trim();
        const port = cfg.get<number>('httpPort') ?? 8765;
        const regKey = (cfg.get<string>('registrationKey') || '').trim();

        const url = `http://${host}:${port}${path}`;
        const headers: Record<string, string> = { 'Content-Type': 'application/json' };
        if (regKey) headers['X-Registration-Key'] = regKey;

        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 5000);
        try {
            const res = await fetch(url, {
                method: init?.method ?? 'GET',
                headers,
                body: init?.body,
                signal: controller.signal,
            });
            const text = await res.text();
            let json: any = null;
            try { json = text ? JSON.parse(text) : null; } catch { json = { raw: text }; }
            if (!res.ok) {
                const msg = (json && (json.error || json.message)) || `HTTP ${res.status}`;
                throw new Error(`${msg} (${res.status})`);
            }
            return json ?? {};
        } finally {
            clearTimeout(timer);
        }
    }

    private async _pushDiscordStatus(): Promise<void> {
        try {
            const r = await this._botHttpFetch('/api/v1/bridge/status');
            this._panel.webview.postMessage({
                type: 'wb.discord.statusResult',
                payload: { ok: true, ...r },
            });
        } catch (err) {
            this._panel.webview.postMessage({
                type: 'wb.discord.statusResult',
                payload: { ok: false, error: String(err) },
            });
        }
    }

    /** 설정 recoder.discord.clientId 로 봇 초대 URL 을 로컬 생성. 없으면 빈 문자열. */
    private _buildLocalInviteUrl(): string {
        const clientId = vscode.workspace
            .getConfiguration('recoder.discord')
            .get<string>('clientId', '')
            .trim();
        if (!clientId) { return ''; }
        const permissions = 2147485696; // 메시지 읽기/쓰기 + 슬래시 커맨드
        return (
            'https://discord.com/api/oauth2/authorize'
            + `?client_id=${encodeURIComponent(clientId)}`
            + `&permissions=${permissions}`
            + '&scope=bot%20applications.commands'
        );
    }

    private async _pushDiscordInviteUrl(): Promise<void> {
        // 설정된 client_id 가 있으면 봇 실행 없이 즉시 초대 URL 표시.
        const local = this._buildLocalInviteUrl();
        if (local) {
            this._panel.webview.postMessage({
                type: 'wb.discord.inviteUrlResult',
                payload: { ok: true, invite_url: local, client_id: 'config' },
            });
            return;
        }
        try {
            const r = await this._botHttpFetch('/api/v1/bridge/invite-url');
            this._panel.webview.postMessage({
                type: 'wb.discord.inviteUrlResult',
                payload: { ok: true, ...r },
            });
        } catch (err) {
            this._panel.webview.postMessage({
                type: 'wb.discord.inviteUrlResult',
                payload: { ok: false, error: String(err) },
            });
        }
    }

    private async _pushDiscordGuilds(): Promise<void> {
        try {
            const r = await this._botHttpFetch('/api/v1/bridge/guilds');
            this._panel.webview.postMessage({
                type: 'wb.discord.guildsResult',
                payload: { ok: true, ...r },
            });
        } catch (err) {
            this._panel.webview.postMessage({
                type: 'wb.discord.guildsResult',
                payload: { ok: false, error: String(err), guilds: [] },
            });
        }
    }

    private async _pushDiscordChannels(guildId: string): Promise<void> {
        try {
            const r = await this._botHttpFetch(
                `/api/v1/bridge/guilds/${encodeURIComponent(guildId)}/channels`,
            );
            this._panel.webview.postMessage({
                type: 'wb.discord.channelsResult',
                payload: { ok: true, ...r },
            });
        } catch (err) {
            this._panel.webview.postMessage({
                type: 'wb.discord.channelsResult',
                payload: { ok: false, error: String(err), guild_id: guildId, channels: [] },
            });
        }
    }

    // ───────── Workbench 풀 구현 헬퍼 ─────────

    /** 현재 열린 첫 워크스페이스 경로. 없으면 빈 문자열. */
    private _getWorkspacePath(): string {
        const folders = vscode.workspace.workspaceFolders;
        if (!folders || folders.length === 0) return '';
        return folders[0].uri.fsPath;
    }

    /**
     * Deploy Center 사전점검 — Core 의 진단/aws/ec2-ready/ecs-ready 결과를 종합해
     * webview 가 표시할 항목 리스트로 반환.
     *
     * 각 항목: { status: 'ok'|'fail'|'warn', name, msg, action?, action_label? }
     */
    private async _collectDeployPrechecks(): Promise<Array<{
        status: 'ok' | 'fail' | 'warn';
        name: string;
        msg?: string;
        action?: string;
        action_label?: string;
    }>> {
        const items: Array<{
            status: 'ok' | 'fail' | 'warn';
            name: string;
            msg?: string;
            action?: string;
            action_label?: string;
        }> = [];

        // 1) 워크스페이스
        const ws = this._getWorkspacePath();
        items.push(ws
            ? { status: 'ok', name: '워크스페이스 열림', msg: ws }
            : { status: 'fail', name: '워크스페이스 없음', msg: 'VS Code 에서 프로젝트 폴더를 여세요.' }
        );

        // 2) Core diagnostics (Docker / AI)
        try {
            const diag = await this._apiClient.getDiagnostics();
            if (diag) {
                const d = diag as unknown as Record<string, string>;
                items.push(d.docker_ready === 'ok'
                    ? { status: 'ok', name: 'Docker daemon 동작' }
                    : { status: 'fail', name: 'Docker daemon 미동작', msg: 'Docker Desktop 을 실행하세요.', action: 'docker_start', action_label: 'Docker Desktop 안내' }
                );
                items.push(d.ai_ready === 'ok'
                    ? { status: 'ok', name: 'Bedrock AI 가용' }
                    : { status: 'warn', name: 'Bedrock AI 미점검', msg: 'AI 분석 기능이 동작하지 않을 수 있습니다.', action: 'core_diagnostics', action_label: '진단 재실행' }
                );
            } else {
                items.push({ status: 'warn', name: 'Core 진단 결과 없음', msg: '진단을 한 번 실행하세요.', action: 'core_diagnostics', action_label: '진단 실행' });
            }
        } catch {
            items.push({ status: 'fail', name: 'Core 통신 실패', msg: '/api/diagnostics 응답 없음' });
        }

        // 3) AWS 자격증명
        try {
            const aws = await this._apiClient.getAwsStatus();
            items.push(aws.ready
                ? { status: 'ok', name: 'AWS 자격증명 유효', msg: aws.identity?.arn ?? '' }
                : { status: 'fail', name: 'AWS 자격증명 미설정', msg: aws.message || 'STS 검증 실패', action: 'aws_configure', action_label: 'AWS 설정' }
            );
        } catch {
            items.push({ status: 'warn', name: 'AWS 상태 확인 불가', msg: '/api/aws/status 응답 없음' });
        }

        // 4) GitHub 연결 (선택 — fail 대신 warn)
        try {
            const gh = await this._apiClient.getGithubStatus();
            const connected = (gh as { status?: string; user?: string }).user
                || (gh as { status?: string }).status === 'connected';
            items.push(connected
                ? { status: 'ok', name: 'GitHub 연결됨', msg: (gh as { user?: string }).user || '' }
                : { status: 'warn', name: 'GitHub 미연결', msg: 'GitHub Actions/푸시 사용 시 필요', action: 'github_login', action_label: '로그인' }
            );
        } catch {
            items.push({ status: 'warn', name: 'GitHub 상태 확인 불가' });
        }

        // 5) EC2/ECS 환경변수 (warn — 폼에 직접 입력해도 됨)
        try {
            const ec2 = await this._apiClient.ec2DeployReady();
            items.push(ec2.ready
                ? { status: 'ok', name: 'EC2 배포 환경 준비됨' }
                : { status: 'warn', name: 'EC2 환경변수 일부 미설정', msg: ec2.issues.slice(0, 2).join(' · ') }
            );
        } catch { /* skip */ }
        try {
            const ecs = await this._apiClient.ecsDeployReady();
            items.push(ecs.ready
                ? { status: 'ok', name: 'ECS 배포 환경 준비됨' }
                : { status: 'warn', name: 'ECS 환경변수 일부 미설정', msg: ecs.issues.slice(0, 2).join(' · ') }
            );
        } catch { /* skip */ }

        return items;
    }

    /** GitHub 상태 조회 후 webview 에 push (chip-github 색상 갱신용). */
    private async _pushGithubStatus(): Promise<void> {
        try {
            const r = await this._apiClient.getGithubStatus(true);
            this._panel.webview.postMessage({
                type: 'wb.gh.statusResult',
                payload: r,
            });
        } catch { /* ignore */ }
    }

    private _ec2StatusTimer: ReturnType<typeof setInterval> | null = null;
    private _ecsStatusTimer: ReturnType<typeof setInterval> | null = null;

    private _startEc2StatusPolling(): void {
        if (this._ec2StatusTimer) return;
        const tick = async () => {
            try {
                const s = await this._apiClient.getEc2DeployStatus();
                this._panel.webview.postMessage({ type: 'wb.deploy.ec2.statusResult', payload: s });
                // log_tail 마지막 줄을 deploy 로그 패널에 출력
                const tail = s.log_tail || [];
                if (tail.length) {
                    this.pushLog('deploy', `[EC2:${s.stage}] ${tail[tail.length - 1]}`);
                }
                if (!s.running) {
                    if (this._ec2StatusTimer) { clearInterval(this._ec2StatusTimer); this._ec2StatusTimer = null; }
                    if (s.stage === 'done') {
                        this.pushLog('deploy', `[OK] EC2 배포 완료 (image=${s.image_uri || '?'})`);
                        this.addActivity('ok', 'EC2 배포 완료');
                    } else if (s.stage === 'failed') {
                        this.pushLog('deploy', `[FAIL] EC2 배포 실패: ${s.error || ''}`);
                        this.addActivity('fail', 'EC2 배포 실패');
                    }
                }
            } catch { /* ignore */ }
        };
        this._ec2StatusTimer = setInterval(() => { void tick(); }, 3000);
        void tick();
    }

    private _startEcsStatusPolling(): void {
        if (this._ecsStatusTimer) return;
        const tick = async () => {
            try {
                const s = await this._apiClient.getEcsDeployStatus();
                this._panel.webview.postMessage({ type: 'wb.deploy.ecs.statusResult', payload: s });
                const tail = s.log_tail || [];
                if (tail.length) {
                    this.pushLog('deploy', `[ECS:${s.stage}] ${tail[tail.length - 1]}`);
                }
                if (!s.running) {
                    if (this._ecsStatusTimer) { clearInterval(this._ecsStatusTimer); this._ecsStatusTimer = null; }
                    if (s.stage === 'done') {
                        this.pushLog('deploy', `[OK] ECS 배포 완료 (image=${s.image_uri || '?'}, task=${s.task_def_arn || '?'})`);
                        this.addActivity('ok', 'ECS 배포 완료');
                    } else if (s.stage === 'failed') {
                        this.pushLog('deploy', `[FAIL] ECS 배포 실패: ${s.error || ''}`);
                        this.addActivity('fail', 'ECS 배포 실패');
                    }
                }
            } catch { /* ignore */ }
        };
        this._ecsStatusTimer = setInterval(() => { void tick(); }, 3000);
        void tick();
    }

    private async _pushHealthAndCost(): Promise<void> {
        try {
            if (!this._coreManager.getSessionToken()) {
                await this._coreManager.refreshToken();
            }
        } catch { /* ignore */ }
        try {
            const last = this._polling.getLastHealth();
            if (last) {
                this._panel.webview.postMessage({ type: 'wb.healthUpdate', payload: last });
            } else {
                void this._polling.poll();
            }
        } catch { /* ignore */ }
        try {
            const cost = await this._apiClient.getCostSummary();
            this._panel.webview.postMessage({ type: 'wb.costUpdate', payload: cost });
        } catch { /* ignore */ }
        try {
            let diag = await this._apiClient.getDiagnostics();
            if (!diag && !this._diagnosticsInflight && !this._diagnosticsLoaded) {
                this._diagnosticsInflight = true;
                try {
                    diag = await this._apiClient.runDiagnostics();
                    this._diagnosticsLoaded = true;
                } finally {
                    this._diagnosticsInflight = false;
                }
            }
            if (diag) {
                this._panel.webview.postMessage({ type: 'wb.diagnosticsUpdate', payload: diag });
            }
        } catch { /* ignore */ }
    }

    private _startPolling(): void {
        if (this._pollTimer) return;
        this._pollTimer = setInterval(() => {
            void this._pushHealthAndCost();
        }, 5000);
    }

    private _startWorkbenchPolling(): void {
        if (this._workbenchPollTimer) return;
        this._workbenchPollTimer = setInterval(() => {
            void this._pollWorkbenchEvents();
        }, 3000);
    }

    private async _pushWorkbenchState(): Promise<void> {
        try {
            const state = await this._apiClient.workbenchState();
            if (state && state.active_mode) {
                this._workbenchMode = state.active_mode;
                this._panel.webview.postMessage({
                    type: 'wb.workbenchState',
                    payload: state,
                });
            }
        } catch { /* ignore */ }
    }

    private async _pollWorkbenchEvents(): Promise<void> {
        try {
            const result = await this._apiClient.workbenchEvents(this._workbenchEventsCursor);
            const rawEvents = (result && Array.isArray(result.events)) ? result.events : [];
            const events = rawEvents as unknown as Array<{
                at: string;
                kind: string;
                source: string;
                payload?: Record<string, unknown>;
            }>;
            const nextOffset = result?.next_offset;
            if (typeof nextOffset === 'number') {
                this._workbenchEventsCursor = nextOffset;
            } else {
                this._workbenchEventsCursor += events.length;
            }
            for (const ev of events) {
                this._renderWorkbenchEvent(ev);
            }
        } catch { /* ignore */ }
    }

    private _renderWorkbenchEvent(ev: {
        at: string;
        kind: string;
        source: string;
        payload?: Record<string, unknown>;
    }): void {
        const sourceLabel = ev.source === 'discord' ? 'Discord' : ev.source === 'vscode' ? 'VSCode' : ev.source;
        let dot: 'ok' | 'warn' | 'fail' | 'info' = 'info';
        let text = '';
        switch (ev.kind) {
            case 'mode_change': {
                const mode = (ev.payload?.mode as string) || 'unknown';
                this._workbenchMode = mode as typeof this._workbenchMode;
                text = `${sourceLabel} → Workbench 모드 전환: ${mode.toUpperCase()}`;
                dot = 'info';
                break;
            }
            case 'preflight': {
                const status = (ev.payload?.status as string) || '';
                const ok = status === 'pass' || status === 'PASS' || ev.payload?.ok === true;
                text = `${sourceLabel} → Preflight 실행 (${ok ? 'PASS' : (status || 'BLOCKED')})`;
                dot = ok ? 'ok' : 'warn';
                break;
            }
            case 'deploy': {
                const id = (ev.payload?.deployment_id as string) || '?';
                text = `${sourceLabel} → 배포 시작 (${id.slice(0, 8)})`;
                dot = 'info';
                break;
            }
            case 'rollback': {
                const id = (ev.payload?.deployment_id as string) || '?';
                text = `${sourceLabel} → Rollback 트리거 (${id.slice(0, 8)})`;
                dot = 'warn';
                break;
            }
            default:
                text = `${sourceLabel} → ${ev.kind}`;
                dot = 'info';
        }
        this.addActivity(dot, text);
        this._panel.webview.postMessage({
            type: 'wb.workbenchEvent',
            payload: {
                at: ev.at,
                kind: ev.kind,
                source: ev.source,
                mode: this._workbenchMode,
                text,
            },
        });
    }

    private _now(): string {
        return new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }

    private _dispose(): void {
        if (this._pollTimer) clearInterval(this._pollTimer);
        this._pollTimer = null;
        if (this._workbenchPollTimer) clearInterval(this._workbenchPollTimer);
        this._workbenchPollTimer = null;
        WorkbenchPanel._current = undefined;
        this._panel.dispose();
    }

    private _renderHtml(webview: vscode.Webview): string {
        return renderWorkbenchHtml(webview, 'panel');
    }
}
