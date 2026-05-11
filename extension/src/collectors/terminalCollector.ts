/**
 * Terminal Output Collector (설계서 v6.4 §8)
 * Shell Integration API로 모든 터미널 명령 출력을 감시.
 * 에러 감지 시 Core에 분석 요청 → 결과를 sidebarCallback으로 전달.
 *
 * ON/OFF 토글: setAutoDetect(boolean)
 */
import * as vscode from 'vscode';
import { CoreManager } from '../core/coreManager';

export class TerminalCollector {
    private _latestOutput: string = '';
    private _autoDetect: boolean = false;
    private _sidebarCallback: ((proposal: object) => void) | null = null;
    private _disposables: vscode.Disposable[] = [];

    constructor(private readonly coreManager: CoreManager) {}

    /** 자동 감지 ON/OFF */
    setAutoDetect(enabled: boolean): void {
        this._autoDetect = enabled;
    }

    /** 분석 결과를 받을 콜백 등록 (extension.ts에서 주입) */
    setSidebarCallback(cb: (proposal: object) => void): void {
        this._sidebarCallback = cb;
    }

    register(context: vscode.ExtensionContext): void {
        if (vscode.window.onDidStartTerminalShellExecution) {
            this._disposables.push(
                vscode.window.onDidStartTerminalShellExecution(async (e) => {
                    if (!this._autoDetect) { return; }

                    const stream = e.execution.read();
                    let output = '';
                    for await (const chunk of stream) {
                        output += chunk;
                    }
                    if (output.trim()) {
                        this._latestOutput = output;
                        await this._onOutputCollected(
                            output,
                            e.execution.commandLine?.value ?? '',
                        );
                    }
                })
            );
        }
        context.subscriptions.push(...this._disposables);
    }

    getLatestOutput(): string {
        return this._latestOutput;
    }

    createRecoderTerminal(): vscode.Terminal {
        const terminal = vscode.window.createTerminal({ name: 'ReCoder Run' });
        terminal.show();
        return terminal;
    }

    async runCommand(command: string): Promise<string> {
        const terminal = this.createRecoderTerminal();
        await new Promise(r => setTimeout(r, 500));
        if (terminal.shellIntegration) {
            const execution = terminal.shellIntegration.executeCommand(command);
            let output = '';
            for await (const chunk of execution.read()) {
                output += chunk;
            }
            return output;
        }
        terminal.sendText(command);
        return '';
    }

    private async _onOutputCollected(output: string, command: string): Promise<void> {
        const errorKeywords = [
            'error', 'Error', 'ERROR',
            'Traceback', 'Exception', 'FAILED',
            'SyntaxError', 'TypeError', 'ValueError',
        ];
        const hasError = errorKeywords.some(kw => output.includes(kw));
        if (!hasError) { return; }

        try {
            await this.coreManager.ensureRunning();
            const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
            const proposal = await this.coreManager.client.analyze({
                workspace_path: workspacePath,
                terminal_output: output,
                error_text: '',
                command,
            });
            if (proposal && this._sidebarCallback) {
                this._sidebarCallback(proposal);
            }
        } catch { /* Core 미실행 또는 분석 실패 — 조용히 무시 */ }
    }

    dispose(): void {
        this._disposables.forEach(d => d.dispose());
    }
}
