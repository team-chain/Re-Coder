/**
 * ReCoder Workbench — Sidebar Provider (옵션 B 핵심 모듈)
 *
 * WebviewViewProvider 구현. Activity Bar 의 ReCoder 컨테이너 아래
 * 새 view 로 등록되어, Primary Sidebar 또는 Secondary Sidebar 어디에든
 * 사용자가 자유롭게 옮길 수 있다 (VSCode 네이티브 drag-and-drop 지원).
 *
 * 기존 Workbench 기능을 제공하는 활성 사이드바 뷰:
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
import { WorkbenchHost } from './workbenchHost';

export class WorkbenchSidebarProvider extends WorkbenchHost implements vscode.WebviewViewProvider {
    public static readonly viewType = 'recoder.workbenchView';

    private _view: vscode.WebviewView | undefined;

    constructor(
        extensionUri: vscode.Uri,
        apiClient: ApiClient,
        coreManager: CoreManager,
        polling: PollingService,
    ) {
        super(extensionUri, apiClient, coreManager, polling);
    }

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
            //: 양방향 sync 타이머도 함께 정리한다. 안 멈추면 뷰가 사라진 뒤에도
            //: 코어를 계속 폴링하고, _post 는 버려지므로 순수한 낭비가 된다.
            if (this._workbenchPollTimer) {
                clearInterval(this._workbenchPollTimer);
                this._workbenchPollTimer = null;
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

        //: Workbench 양방향 sync — 패널에만 있고 사이드바에는 없었다. 실제로
        //: 렌더되는 쪽이 사이드바이므로, 이게 없으면 Core/Discord 에서 올라온
        //: 이벤트가 화면에 영영 안 뜬다.
        void this._pushWorkbenchState();
        this._startWorkbenchPolling();
    }

    /** 사이드바 뷰의 웹뷰로 전송. 뷰가 아직 없으면(접힘/미해결) 버린다. */
    protected _post(msg: { type: string; payload?: any }): void {
        if (this._view) {
            this._view.webview.postMessage(msg);
        }
    }

    /** 보이는 동안만 폴링 — 접혀 있을 때 코어를 두드리지 않는다. */
    protected _startPolling(): void {
        if (this._pollTimer) { return; }
        this._pollTimer = setInterval(() => {
            if (this._view?.visible) {
                void this._pushHealthAndCost();
            }
        }, 5000);
    }
}
