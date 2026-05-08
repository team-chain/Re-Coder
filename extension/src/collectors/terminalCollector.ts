/**
 * Terminal Output Collector (설계서 v6.4 §8)
 * 1순위: TerminalShellIntegration API
 * 2순위: onDidStartTerminalShellExecution 이벤트
 * 3순위: recoder run 래퍼 터미널
 * 4순위: 수동 붙여넣기 (Sidebar에서 처리)
 */
import * as vscode from 'vscode';
import { CoreManager } from '../core/coreManager';

export class TerminalCollector {
    private _latestOutput: string = '';
    private _disposables: vscode.Disposable[] = [];

    constructor(private readonly coreManager: CoreManager) {}

    register(context: vscode.ExtensionContext): void {
        // 2순위: Shell Execution 이벤트
        if (vscode.window.onDidStartTerminalShellExecution) {
            this._disposables.push(
                vscode.window.onDidStartTerminalShellExecution(async (e) => {
                    const stream = e.execution.read();
                    let output = '';
                    for await (const chunk of stream) {
                        output += chunk;
                    }
                    if (output.trim()) {
                        this._latestOutput = output;
                        await this._onOutputCollected(output, e.execution.commandLine?.value ?? '');
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
        // 1순위: ReCoder 전용 터미널 (Run with ReCoder 버튼)
        // shellIntegration 은 VSCode 가 자동으로 활성화하므로 별도 옵션 불필요
        const terminal = vscode.window.createTerminal({
            name: 'ReCoder Run',
        });
        terminal.show();
        return terminal;
    }

    async runCommand(command: string): Promise<string> {
        // TerminalShellIntegration.executeCommand 사용
        const terminal = this.createRecoderTerminal();
        await new Promise(r => setTimeout(r, 500)); // shell 준비 대기
        if (terminal.shellIntegration) {
            const execution = terminal.shellIntegration.executeCommand(command);
            let output = '';
            const stream = execution.read();
            for await (const chunk of stream) {
                output += chunk;
            }
            return output;
        }
        // fallback: sendText
        terminal.sendText(command);
        return '';
    }

    private async _onOutputCollected(output: string, command: string): Promise<void> {
        // 에러 키워드 감지 → Core에 분석 요청
        const errorKeywords = ['error', 'Error', 'ERROR', 'Traceback', 'Exception', 'FAILED'];
        const hasError = errorKeywords.some(kw => output.includes(kw));
        if (!hasError) return;

        try {
            const client = this.coreManager.client;
            const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
            await client.analyze({
                workspace_path: workspacePath,
                terminal_output: output,
                command,
            });
        } catch { /* Core 미실행 시 무시 */ }
    }


    dispose(): void {
        this._disposables.forEach(d => d.dispose());
    }
}
