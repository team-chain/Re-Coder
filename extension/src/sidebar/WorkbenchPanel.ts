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
import { WorkbenchHost } from './workbenchHost';

export class WorkbenchPanel extends WorkbenchHost {
    public static readonly viewType = 'recoder.workbench';
    private static _current: WorkbenchPanel | undefined;

    private readonly _panel: vscode.WebviewPanel;

    // ── Workbench bidirectional sync (Discord ↔ Core ↔ VSCode) ──────────
    /** /workbench/events cursor — index offset into Core's in-memory event buffer
     *  (NOT a timestamp). Each /events response carries next_offset which we use
     *  for the next call. Starts at 0 = "give me everything from the beginning". */
    /** Current workbench mode (cached, last seen from Core). */

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
        extensionUri: vscode.Uri,
        apiClient: ApiClient,
        coreManager: CoreManager,
        polling: PollingService,
    ) {
        super(extensionUri, apiClient, coreManager, polling);
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


    /** 에디터 영역 패널의 웹뷰로 전송. */
    protected _post(msg: { type: string; payload?: any }): void {
        this._panel.webview.postMessage(msg);
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
