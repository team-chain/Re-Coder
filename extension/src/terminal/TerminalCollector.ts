import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { TerminalOutput } from '../types';
import { ApiClient } from '../core/ApiClient';

const execFileAsync = promisify(execFile);

const ERROR_PATTERNS: RegExp[] = [
    /\bTraceback\s+\(most recent call last\)/i,
    /\bError\b.*:/i,
    /\bException\b.*:/i,
    /\bFATAL\b/i,
    /\bSyntaxError\b/i,
    /\bTypeError\b/i,
    /\bReferenceError\b/i,
    /\bNullPointerException\b/i,
    /\bSegmentation fault\b/i,
    /\bpanic:/i,
    /\bFailed to compile/i,
    /\bBuild failed/i,
    /npm ERR!/i,
    /yarn error/i,
    /exit code [1-9][0-9]*/i,
    /ENOENT|EACCES|ECONNREFUSED/i,
];

export class TerminalCollector {
    private outputBuffer: Map<string, string[]> = new Map();
    private apiClient: ApiClient | null = null;

    constructor(apiClient?: ApiClient) {
        if (apiClient) { this.apiClient = apiClient; }
    }

    setApiClient(client: ApiClient): void {
        this.apiClient = client;
    }

    async createReCoderTerminal(command: string): Promise<TerminalOutput> {
        const start = Date.now();
        const tmpOutput = path.join(os.tmpdir(), `recoder-out-${Date.now()}.txt`);

        const hasShellIntegration = vscode.window.terminals.some(
            (t) => t.shellIntegration !== undefined
        );

        if (hasShellIntegration) {
            return this.runWithShellIntegration(command);
        }

        const wrappedCmd =
            process.platform === 'win32'
                ? `${command} > "${tmpOutput}" 2>&1`
                : `${command} 2>&1 | tee "${tmpOutput}"`;

        const terminal = vscode.window.createTerminal({ name: 'ReCoder', hideFromUser: false });
        terminal.show(false);
        terminal.sendText(wrappedCmd);

        const output = await this.waitForOutputFile(tmpOutput, 60000);

        const termOutput: TerminalOutput = {
            command,
            output,
            exitCode: 0,
            timestamp: new Date().toISOString(),
        };

        if (this.detectError(output)) { await this.triggerAnalysis(termOutput); }

        try { fs.unlinkSync(tmpOutput); } catch { /* ignore */ }

        return termOutput;
    }

    registerShellIntegrationListeners(
        context: vscode.ExtensionContext,
        onOutput: (output: TerminalOutput) => void
    ): void {
        if ('onDidStartTerminalShellExecution' in vscode.window) {
            const startDisposable = (
                vscode.window as typeof vscode.window & {
                    onDidStartTerminalShellExecution: (
                        listener: (e: { terminal: vscode.Terminal; execution: { commandLine: { value: string } } }) => void
                    ) => vscode.Disposable;
                }
            ).onDidStartTerminalShellExecution((e) => {
                const terminalId = this.getTerminalId(e.terminal);
                if (!this.outputBuffer.has(terminalId)) {
                    this.outputBuffer.set(terminalId, []);
                }
            });
            context.subscriptions.push(startDisposable);
        }

        if ('onDidEndTerminalShellExecution' in vscode.window) {
            const endDisposable = (
                vscode.window as typeof vscode.window & {
                    onDidEndTerminalShellExecution: (
                        listener: (e: {
                            terminal: vscode.Terminal;
                            exitCode: number | undefined;
                            execution: {
                                commandLine: { value: string };
                                read: () => AsyncIterable<string>;
                            };
                        }) => void
                    ) => vscode.Disposable;
                }
            ).onDidEndTerminalShellExecution(async (e) => {
                const lines: string[] = [];
                try {
                    for await (const data of e.execution.read()) { lines.push(data); }
                } catch { /* ignore */ }

                const terminalId = this.getTerminalId(e.terminal);
                this.outputBuffer.set(terminalId, lines);

                const output = lines.join('');
                const termOutput: TerminalOutput = {
                    command: e.execution.commandLine.value,
                    output,
                    exitCode: e.exitCode ?? 0,
                    timestamp: new Date().toISOString(),
                };

                onOutput(termOutput);
                if (this.detectError(output)) { await this.triggerAnalysis(termOutput); }
            });
            context.subscriptions.push(endDisposable);
        }

        const dataDisposable = (vscode.window as any).onDidWriteTerminalData?.((e: any) => {
            const terminalId = this.getTerminalId(e.terminal);
            const buf = this.outputBuffer.get(terminalId) ?? [];
            buf.push(e.data);
            if (buf.length > 500) { buf.splice(0, buf.length - 500); }
            this.outputBuffer.set(terminalId, buf);
        });
        if (dataDisposable) { context.subscriptions.push(dataDisposable); }
    }

    async runWithFallback(command: string, workspacePath: string): Promise<TerminalOutput> {
        const shell = process.platform === 'win32' ? 'cmd.exe' : '/bin/sh';
        const shellFlag = process.platform === 'win32' ? '/c' : '-c';

        try {
            const { stdout, stderr } = await execFileAsync(shell, [shellFlag, command], {
                cwd: workspacePath,
                maxBuffer: 1024 * 1024 * 10,
                timeout: 120000,
            });

            const combined = stdout + (stderr ? `\nSTDERR:\n${stderr}` : '');
            const termOutput: TerminalOutput = {
                command, output: combined, exitCode: 0, timestamp: new Date().toISOString(),
            };
            if (this.detectError(combined)) { await this.triggerAnalysis(termOutput); }
            return termOutput;
        } catch (err: unknown) {
            const execErr = err as { stdout?: string; stderr?: string; code?: number; message?: string };
            const combined =
                (execErr.stdout ?? '') +
                (execErr.stderr ? `\nSTDERR:\n${execErr.stderr}` : '') +
                (execErr.message ? `\n${execErr.message}` : '');
            const termOutput: TerminalOutput = {
                command, output: combined, exitCode: execErr.code ?? 1, timestamp: new Date().toISOString(),
            };
            await this.triggerAnalysis(termOutput);
            return termOutput;
        }
    }

    async promptManualInput(): Promise<string | null> {
        const result = await vscode.window.showInputBox({
            prompt: '에러 로그를 붙여넣으세요 (긴 내용은 Ctrl+V)',
            placeHolder: 'Error: ...',
            ignoreFocusOut: true,
        });
        return result ?? null;
    }

    /**
     * Shell Integration이 활성화된 터미널을 등록합니다.
     * extension.ts의 onDidChangeTerminalShellIntegration 핸들러에서 호출됩니다.
     */
    attachShellIntegration(
        terminal: vscode.Terminal,
        _shellIntegration: vscode.TerminalShellIntegration
    ): void {
        const terminalId = this.getTerminalId(terminal);
        if (!this.outputBuffer.has(terminalId)) {
            this.outputBuffer.set(terminalId, []);
        }
        console.log(`[TerminalCollector] Shell Integration attached: ${terminal.name}`);
    }

    /**
     * onDidWriteTerminalData 이벤트 핸들러 — 데이터를 버퍼에 저장합니다.
     */
    onTerminalData(terminal: vscode.Terminal, data: string): void {
        const terminalId = this.getTerminalId(terminal);
        const buf = this.outputBuffer.get(terminalId) ?? [];
        buf.push(data);
        if (buf.length > 500) { buf.splice(0, buf.length - 500); }
        this.outputBuffer.set(terminalId, buf);
    }

    /**
     * 활성 터미널(또는 가장 최근 터미널)의 마지막 출력을 반환합니다.
     */
    getLatestOutput(lines: number = 100): string {
        const activeTerm = vscode.window.activeTerminal;
        if (activeTerm) {
            const id = this.getTerminalId(activeTerm);
            if (this.outputBuffer.has(id)) { return this.getLastOutput(id, lines); }
        }
        const entries = [...this.outputBuffer.entries()];
        if (entries.length === 0) { return ''; }
        const [lastId] = entries[entries.length - 1];
        return this.getLastOutput(lastId, lines);
    }

    getLastOutput(terminalId: string, lines: number = 100): string {
        const buf = this.outputBuffer.get(terminalId) ?? [];
        const allText = buf.join('');
        const allLines = allText.split('\n');
        return allLines.slice(-lines).join('\n');
    }

    detectError(output: string): boolean {
        return ERROR_PATTERNS.some((pattern) => pattern.test(output));
    }

    private async triggerAnalysis(output: TerminalOutput): Promise<void> {
        if (!this.apiClient) { return; }
        const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        try {
            await this.apiClient.analyze({
                workspace_path: workspacePath,
                terminal_output: output.output,
                command: output.command,
            });
        } catch (err) {
            console.error('[TerminalCollector] Auto-analysis failed:', err);
        }
    }

    private async runWithShellIntegration(command: string): Promise<TerminalOutput> {
        return new Promise((resolve) => {
            const terminal = vscode.window.createTerminal({ name: 'ReCoder' });
            terminal.show(false);
            let outputLines: string[] = [];

            const dataDisposable = (vscode.window as any).onDidWriteTerminalData?.((e: any) => {
                if (e.terminal === terminal) { outputLines.push(e.data); }
            });

            terminal.sendText(command);

            setTimeout(() => {
                dataDisposable?.dispose();
                const output = outputLines.join('');
                resolve({
                    command, output, exitCode: 0, timestamp: new Date().toISOString(),
                });
            }, 5000);
        });
    }

    private async waitForOutputFile(filePath: string, timeoutMs: number): Promise<string> {
        const start = Date.now();
        while (Date.now() - start < timeoutMs) {
            if (fs.existsSync(filePath)) {
                const content = fs.readFileSync(filePath, 'utf-8');
                if (content.length > 0) { return content; }
            }
            await new Promise((r) => setTimeout(r, 500));
        }
        return '';
    }

    private getTerminalId(terminal: vscode.Terminal): string {
        return `${terminal.name}-${((terminal as unknown) as { processId?: unknown }).processId ?? 'unknown'}`;
    }
}
