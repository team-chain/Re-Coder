/**
 * ReCoder Workbench — Sidebar Provider (옵션 B 핵심 모듈)
 *
 * WebviewViewProvider 구현. Activity Bar 의 ReCoder 컨테이너 아래
 * 새 view 로 등록되어, Primary Sidebar 또는 Secondary Sidebar 어디에든
 * 사용자가 자유롭게 옮길 수 있다 (VSCode 네이티브 drag-and-drop 지원).
 *
 * WorkbenchPanel (Editor Area) 과 같은 HTML / 메시지 프로토콜을 사용하지만:
 *   - 폭이 좁은 환경 — workbenchHtml.ts 가 자동으로 단일 컬럼으로 전환 (data-mode="sidebar")
 *   - 항상 보임 — Kiro 스타일의 "옆에 두고 코드 편집" 워크플로우
 *
 * 사용:
 *   await vscode.commands.executeCommand('recoder.workbenchView.focus');
 */
import * as vscode from 'vscode';
import { CoreManager } from '../core/CoreManager';
import { ApiClient } from '../core/ApiClient';
import { PollingService } from '../core/PollingService';
import { renderWorkbenchHtml } from './workbenchHtml';

export class WorkbenchSidebarProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'recoder.workbenchView';

    private _view: vscode.WebviewView | undefined;
    private _activity: { dot: string; text: string; time: string }[] = [];
    private _pollTimer: ReturnType<typeof setInterval> | null = null;
    private _diagnosticsInflight: boolean = false;
    private _diagnosticsLoaded: boolean = false;

    constructor(
        private readonly _extensionUri: vscode.Uri,
        private readonly _apiClient: ApiClient,
        private readonly _coreManager: CoreManager,
        private readonly _polling: PollingService,
    ) {}

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ): void {
        this._view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [vscode.Uri.joinPath(this._extensionUri, 'media')],
        };

        webviewView.webview.html = renderWorkbenchHtml(webviewView.webview, 'sidebar');

        webviewView.webview.onDidReceiveMessage(async (msg: { type: string; payload?: any }) => {
            await this._handleMessage(msg);
        });

        webviewView.onDidDispose(() => {
            if (this._pollTimer) {
                clearInterval(this._pollTimer);
                this._pollTimer = null;
            }
            this._view = undefined;
        });

        // visibility 변화 — 사용자가 view 를 숨겼다가 다시 보면 즉시 갱신
        webviewView.onDidChangeVisibility(() => {
            if (webviewView.visible) {
                void this._pushHealthAndCost();
            }
        });

        // 초기 push + 폴링 시작
        setTimeout(() => this._pushHealthAndCost().catch(() => {}), 800);
        this._startPolling();
    }

    public addActivity(dotClass: 'ok' | 'warn' | 'fail' | 'info', text: string): void {
        const item = { dot: dotClass, text, time: this._now() };
        this._activity.unshift(item);
        if (this._activity.length > 30) this._activity.pop();
        this._post({ type: 'wb.activity', payload: { items: this._activity } });
    }

    public pushLog(pane: 'ai' | 'docker' | 'github' | 'deploy' | 'health', line: string): void {
        this._post({ type: 'wb.log', payload: { pane, line } });
    }

    public pushDiagnostics(diag: import('../types').DiagnosticsResult): void {
        this._post({ type: 'wb.diagnosticsUpdate', payload: diag });
    }

    // ──────────────────────────────────────────────────────────────────

    private async _handleMessage(msg: { type: string; payload?: any }): Promise<void> {
        switch (msg.type) {
            case 'wb.ready':
                await this._pushHealthAndCost();
                this._post({ type: 'wb.activity', payload: { items: this._activity } });
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
            default:
                console.warn('[WorkbenchSidebar] Unknown message:', msg.type);
        }
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
                this._post({ type: 'wb.healthUpdate', payload: last });
            } else {
                void this._polling.poll();
            }
        } catch { /* ignore */ }

        try {
            const cost = await this._apiClient.getCostSummary();
            this._post({ type: 'wb.costUpdate', payload: cost });
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
                this._post({ type: 'wb.diagnosticsUpdate', payload: diag });
            }
        } catch { /* ignore */ }
    }

    private _startPolling(): void {
        if (this._pollTimer) return;
        this._pollTimer = setInterval(() => {
            // visible 일 때만 폴링 (백그라운드 비용 절감)
            if (this._view?.visible) {
                void this._pushHealthAndCost();
            }
        }, 5000);
    }

    private _post(msg: { type: string; payload?: any }): void {
        if (this._view) {
            this._view.webview.postMessage(msg);
        }
    }

    private _now(): string {
        return new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }
}
