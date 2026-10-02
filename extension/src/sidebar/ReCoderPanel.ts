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
    ) {
        // dispose 콜백에서는 panel.webview 접근이 금지된다. 미리 참조를 보관해야
        // 창을 닫은 뒤에도 정리 로직이 예외 없이 실행되고 다음 창을 열 수 있다.
        const webview = this._panel.webview;
        webview.html = this._sidebarProvider.getWorkspacePanelHtml(webview);
        this._sidebarProvider.attachWorkspacePanel(webview);

        this._hideSidebar();

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
    static revive(panel: vscode.WebviewPanel, extensionUri: vscode.Uri, sidebarProvider: SidebarProvider): ReCoderPanel {
        if (ReCoderPanel._current) {
            //: 새 확장이 이미 ReCoder 창을 열었다 — 되살린 탭은 중복이라 닫는다.
            panel.dispose();
            ReCoderPanel._current._panel.reveal(undefined, true);
            return ReCoderPanel._current;
        }
        panel.webview.options = ReCoderPanel.webviewOptions(extensionUri);
        ReCoderPanel._current = new ReCoderPanel(panel, sidebarProvider);
        return ReCoderPanel._current;
    }

    static createOrShow(extensionUri: vscode.Uri, sidebarProvider: SidebarProvider): ReCoderPanel {
        const column = vscode.window.activeTextEditor?.viewColumn ?? vscode.ViewColumn.One;
        if (ReCoderPanel._current) {
            ReCoderPanel._current._panel.reveal(column, false);
            ReCoderPanel._current._hideSidebar();
            return ReCoderPanel._current;
        }

        const panel = vscode.window.createWebviewPanel(
            ReCoderPanel.viewType,
            'ReCoder',
            column,
            { ...ReCoderPanel.webviewOptions(extensionUri), retainContextWhenHidden: true },
        );

        ReCoderPanel._current = new ReCoderPanel(panel, sidebarProvider);
        return ReCoderPanel._current;
    }
}
