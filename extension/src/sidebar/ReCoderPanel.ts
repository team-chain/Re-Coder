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

        // 큰 ReCoder 작업 화면을 단독 창처럼 사용한다. VS Code의 Sidebar는
        // 하나뿐이므로 ReCoder만 숨길 수는 없고, 설정이 켜져 있을 때 전체를 닫는다.
        if (vscode.workspace.getConfiguration('recoder.workspace').get<boolean>('hideSidebar', true)) {
            setTimeout(() => {
                void vscode.commands.executeCommand('workbench.action.closeSidebar');
            }, 0);
        }

        this._panel.onDidDispose(() => {
            this._sidebarProvider.detachWorkspacePanel(webview);
            ReCoderPanel._current = undefined;
        });
    }

    static createOrShow(extensionUri: vscode.Uri, sidebarProvider: SidebarProvider): ReCoderPanel {
        const column = vscode.window.activeTextEditor?.viewColumn ?? vscode.ViewColumn.One;
        if (ReCoderPanel._current) {
            ReCoderPanel._current._panel.reveal(column, true);
            return ReCoderPanel._current;
        }

        const panel = vscode.window.createWebviewPanel(
            ReCoderPanel.viewType,
            'ReCoder',
            column,
            {
                enableScripts: true,
                retainContextWhenHidden: true,
                localResourceRoots: [
                    vscode.Uri.joinPath(extensionUri, 'media'),
                    vscode.Uri.joinPath(extensionUri, 'out'),
                ],
            },
        );

        ReCoderPanel._current = new ReCoderPanel(panel, sidebarProvider);
        return ReCoderPanel._current;
    }
}
