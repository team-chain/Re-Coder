/**
 * ReCoder Workspace Panel
 *
 * 사이드바 전용 UI 대신 에디터 영역에 열리는 큰 작업 화면이다.
 * 화면 내부에서는 SidebarProvider 의 기존 상태와 명령을 그대로 공유하며,
 * React 앱이 왼쪽 작업 영역 / 오른쪽 AI 대화 영역으로 레이아웃을 전환한다.
 */
import * as vscode from 'vscode';
import { SidebarProvider } from './SidebarProvider';

export class ReCoderPanel {
    public static readonly viewType = 'recoder.workspace';
    private static _current: ReCoderPanel | undefined;

    private constructor(
        private readonly _panel: vscode.WebviewPanel,
        private readonly _sidebarProvider: SidebarProvider,
        hideSidebar = true,
    ) {
        // dispose 콜백에서는 panel.webview 접근이 금지된다. 미리 참조를 보관해야
        // 창을 닫은 뒤에도 정리 로직이 예외 없이 실행되고 다음 창을 열 수 있다.
        const webview = this._panel.webview;
        webview.html = this._sidebarProvider.getWorkspacePanelHtml(webview);
        this._sidebarProvider.attachWorkspacePanel(webview);

        if (hideSidebar) { this._hideSidebar(); }

        this._panel.onDidDispose(() => {
            this._sidebarProvider.detachWorkspacePanel(webview);
            ReCoderPanel._current = undefined;
        });
    }

    private _hideSidebar(): void {
        // Also close it on re-entry, so the next Activity Bar click reveals the view again
        // instead of merely toggling an already visible sidebar closed.
        if (vscode.workspace.getConfiguration('recoder.workspace').get<boolean>('hideSidebar', true)) {
            setTimeout(() => {
                void vscode.commands.executeCommand('workbench.action.closeSidebar');
            }, 0);
        }
    }

    /** 패널을 만들 때와 되살릴 때 같은 옵션을 쓴다. */
    static webviewOptions(extensionUri: vscode.Uri): vscode.WebviewOptions {
        return {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.joinPath(extensionUri, 'media'),
                vscode.Uri.joinPath(extensionUri, 'out'),
            ],
        };
    }

    /**
     * 확장이 다시 시작된 뒤 VS Code 가 남겨 둔 ReCoder 탭을 새 확장에 다시 붙인다.
     *
     * 되살리기(serializer)가 없으면 확장 재시작(VSIX 설치 후 "확장 다시 시작", 빈 창에
     * 폴더 추가 등) 뒤에도 옛 탭이 화면에 그대로 남는데, 그 탭의 메시지를 받을 확장이
     * 없다. 사용자가 그 탭에서 요청을 보내면 30초 뒤 "확장이 요청을 받지 못했습니다"가
     * 떴다. 이제는 그 탭을 새 확장에 연결하고 화면을 새로 그린다.
     */
    static revive(panel: vscode.WebviewPanel, extensionUri: vscode.Uri, sidebarProvider: SidebarProvider, state?: unknown): ReCoderPanel {
        if (ReCoderPanel._current) {
            //: 새 확장이 이미 ReCoder 창을 열었다 — 되살린 탭은 중복이라 닫는다.
            panel.dispose();
            ReCoderPanel._current._panel.reveal(undefined, true);
            return ReCoderPanel._current;
        }
        panel.webview.options = ReCoderPanel.webviewOptions(extensionUri);
        //: 웹뷰가 남긴 화면 상태(보던 화면·쓰던 요청) — 새로 그린 화면이 ready 를 보내면 돌려준다.
        sidebarProvider.setRestoredUiState(state);
        ReCoderPanel._current = new ReCoderPanel(panel, sidebarProvider);
        return ReCoderPanel._current;
    }

    /** 특정 화면(예: 운영 대응)을 열어 보여 준다 — 새 창이면 화면이 준비될 때 그 화면으로 시작한다. */
    static showView(extensionUri: vscode.Uri, sidebarProvider: SidebarProvider, view: string, extra?: Record<string, unknown>, opts?: ShowOptions): void {
        if (ReCoderPanel._current) {
            ReCoderPanel.createOrShow(extensionUri, sidebarProvider, opts);
            void ReCoderPanel._current._panel.webview.postMessage({ type: 'ui.openView', payload: { view, ...(extra ?? {}) } });
            return;
        }
        sidebarProvider.requestView(view, extra);
        ReCoderPanel.createOrShow(extensionUri, sidebarProvider, opts);
    }

    /** 창이 이미 있으면 앞으로 꺼내고 true, 없으면 아무것도 하지 않고 false. */
    static createOrShowExisting(extensionUri: vscode.Uri, sidebarProvider: SidebarProvider, opts?: ShowOptions): boolean {
        if (!ReCoderPanel._current) { return false; }
        ReCoderPanel.createOrShow(extensionUri, sidebarProvider, opts);
        return true;
    }

    /** 열린 ReCoder 창에 메시지를 보낸다(없으면 false). */
    static post(type: string, payload: unknown): boolean {
        if (!ReCoderPanel._current) { return false; }
        void ReCoderPanel._current._panel.webview.postMessage({ type, payload });
        return true;
    }

    static createOrShow(extensionUri: vscode.Uri, sidebarProvider: SidebarProvider, opts: ShowOptions = {}): ReCoderPanel {
        const column = vscode.window.activeTextEditor?.viewColumn ?? vscode.ViewColumn.One;
        if (ReCoderPanel._current) {
            //: stay — 빠른 이동: 다른 편집기 그룹에 둔 ReCoder 탭을 지금 그룹으로 끌고 오지 않는다(그 자리에서 앞으로).
            ReCoderPanel._current._panel.reveal(opts.stay ? undefined : column, !!opts.preserveFocus);
            if (opts.hideSidebar !== false) { ReCoderPanel._current._hideSidebar(); }
            return ReCoderPanel._current;
        }

        const panel = vscode.window.createWebviewPanel(
            ReCoderPanel.viewType,
            'ReCoder',
            { viewColumn: column, preserveFocus: !!opts.preserveFocus },
            { ...ReCoderPanel.webviewOptions(extensionUri), retainContextWhenHidden: true },
        );

        ReCoderPanel._current = new ReCoderPanel(panel, sidebarProvider, opts.hideSidebar !== false);
        return ReCoderPanel._current;
    }
}

export interface ShowOptions {
    /** 이미 열린 탭을 그 편집기 그룹에 둔 채 앞으로 꺼낸다(기본: 지금 편집기 그룹으로 옮김). */
    stay?: boolean;
    /** false 면 recoder.workspace.hideSidebar 설정이 켜져 있어도 사이드바를 닫지 않는다. */
    hideSidebar?: boolean;
    /** 창을 보여 주되 키보드 포커스는 지금 자리(치던 편집기)에 둔다 — 빠른 이동 휠. */
    preserveFocus?: boolean;
}
