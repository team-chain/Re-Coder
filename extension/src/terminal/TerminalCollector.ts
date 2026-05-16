import * as vscode from 'vscode';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { TerminalOutput } from '../types';
import { ApiClient } from '../core/ApiClient';

const execFileAsync = promisify(execFile);

// ---------------------------------------------------------------------------
// Error pattern matching (§8.2)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// TerminalCollector
// ---------------------------------------------------------------------------

export class TerminalCollector {
    /** Per-terminal output buffer — keyed by stable WeakMap-assigned ID */
    private outputBuffer: Map<string, string[]> = new Map();

    /**
     * WeakMap assigns a stable string ID to each Terminal instance.
     * Avoids the pitfall of `terminal.processId` being a Thenable<number>
     * (not a sync number), which would coerce to "[object Promise]" and
     * cause every terminal to share the same buffer key.
     */
    private terminalIds: WeakMap<vscode.Terminal, string> = new WeakMap();
    private terminalIdCounter: number = 0;

    private apiClient: ApiClient | null = null;

    constructor(apiClient?: ApiClient) {
        if (apiClient) { this.apiClient = apiClient; }
    }

    setApiClient(client: ApiClient): void {
        this.apiClient = client;
    }

    // -----------------------------------------------------------------------
    // Shell Integration listeners (§8.1 — primary collection method)
    // -----------------------------------------------------------------------

    /**
     * Register VSCode Shell Integration listeners.
     *
     * Must be called ONCE from extension.ts activate() so that
     * onDidEndTerminalShellExecution is wired up and output actually flows
     * into the buffer.
     *
     * @param context   Extension context (for subscription cleanup)
     * @param onOutput  Callback invoked after EVERY command completes.
     *                  The caller (extension.ts) decides whether to trigger
     *                  sidebar analysis — do NOT duplicate that logic here.
     */
    registerShellIntegrationListeners(
        context: vscode.ExtensionContext,
        onOutput: (output: TerminalOutput) => void
    ): void {
        // onDidStartTerminalShellExecution — stable in VSCode ≥ 1.93
        if ('onDidStartTerminalShellExecution' in vscode.window) {
            const startDisposable = (
                vscode.window as typeof vscode.window & {
                    onDidStartTerminalShellExecution: (
                        listener: (e: {
                            terminal: vscode.Terminal;
                            execution: { commandLine: { value: string } };
                        }) => void
                    ) => vscode.Disposable;
                }
            ).onDidStartTerminalShellExecution((e) => {
                const id = this.getTerminalId(e.terminal);
                if (!this.outputBuffer.has(id)) {
                    this.outputBuffer.set(id, []);
                }
            });
            context.subscriptions.push(startDisposable);
        }

        // onDidEndTerminalShellExecution — stable in VSCode ≥ 1.93
        // This is the ONLY reliable way to capture shell output without the
        // terminalDataWriteEvent proposed API.
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
                    for await (const chunk of e.execution.read()) { lines.push(chunk); }
                } catch { /* ignore read errors */ }

                const id = this.getTerminalId(e.terminal);
                this.outputBuffer.set(id, lines);

                const termOutput: TerminalOutput = {
                    command: e.execution.commandLine.value,
                    output: lines.join(''),
                    exitCode: e.exitCode ?? 0,
                    timestamp: new Date().toISOString(),
                };

                // Notify the caller — extension.ts decides whether to show in sidebar.
                // Do NOT call this.triggerAnalysis() here; that would fire a raw API
                // call with no sidebar update AND potentially double-trigger if the
                // caller also requests analysis.
                onOutput(termOutput);
            });
            context.subscriptions.push(endDisposable);
        }

        // NOTE: onDidWriteTerminalData (proposed API — terminalDataWriteEvent)
        // is intentionally NOT used. It requires enabledApiProposals declaration
        // and --enable-proposed-api flag. Shell Integration (above) is the
        // production-safe alternative per §8.1.
    }

    /**
     * Called from extension.ts onDidChangeTerminalShellIntegration.
     * Initialises an empty buffer slot for the terminal so getLatestOutput()
     * can return an entry for it even before the first command runs.
     */
    attachShellIntegration(
        terminal: vscode.Terminal,
        _shellIntegration: vscode.TerminalShellIntegration
    ): void {
        const id = this.getTerminalId(terminal);
        if (!this.outputBuffer.has(id)) {
            this.outputBuffer.set(id, []);
        }
        console.log(`[TerminalCollector] Shell Integration attached: ${terminal.name}`);
    }

    // -----------------------------------------------------------------------
    // Run with ReCoder (§8.3 — explicit command execution)
    // -----------------------------------------------------------------------

    /**
     * Run *command* as a child process and return its combined output.
     *
     * Uses execFileAsync (not a visible VSCode terminal) so stdout/stderr are
     * captured reliably without any proposed API dependency.
     * The caller (extension.ts runWithRecoder) passes output to the sidebar.
     */
    async createReCoderTerminal(command: string): Promise<TerminalOutput> {
        const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        return this.runWithFallback(command, workspacePath);
    }

    /**
     * Run *command* via the system shell and return captured output.
     * Used both by createReCoderTerminal and as a direct fallback.
     */
    async runWithFallback(command: string, workspacePath: string): Promise<TerminalOutput> {
        const shell = process.platform === 'win32' ? 'cmd.exe' : '/bin/sh';
        const shellFlag = process.platform === 'win32' ? '/c' : '-c';

        try {
            const { stdout, stderr } = await execFileAsync(shell, [shellFlag, command], {
                cwd: workspacePath || undefined,
                maxBuffer: 1024 * 1024 * 10, // 10 MB
                timeout: 120_000,             // 2 min hard cap
            });

            const combined = stdout + (stderr ? `\nSTDERR:\n${stderr}` : '');
            return {
                command,
                output: combined,
                exitCode: 0,
                timestamp: new Date().toISOString(),
            };
        } catch (err: unknown) {
            const execErr = err as {
                stdout?: string;
                stderr?: string;
                code?: number;
                message?: string;
            };
            const combined =
                (execErr.stdout ?? '') +
                (execErr.stderr ? `\nSTDERR:\n${execErr.stderr}` : '') +
                (execErr.message ? `\n${execErr.message}` : '');
            return {
                command,
                output: combined,
                exitCode: execErr.code ?? 1,
                timestamp: new Date().toISOString(),
            };
        }
    }

    // -----------------------------------------------------------------------
    // Buffer access
    // -----------------------------------------------------------------------

    /**
     * Return the last *lines* lines from the active terminal's output buffer.
     * Falls back to the most recently updated terminal if no terminal is active.
     */
    getLatestOutput(lines: number = 100): string {
        const activeTerm = vscode.window.activeTerminal;
        if (activeTerm) {
            const id = this.getTerminalId(activeTerm);
            if (this.outputBuffer.has(id)) {
                return this.getLastLines(id, lines);
            }
        }
        // No active terminal with a buffer — use the most recently written one
        const entries = [...this.outputBuffer.entries()];
        if (entries.length === 0) { return ''; }
        const [lastId] = entries[entries.length - 1];
        return this.getLastLines(lastId, lines);
    }

    getLastOutput(terminalId: string, lines: number = 100): string {
        return this.getLastLines(terminalId, lines);
    }

    // -----------------------------------------------------------------------
    // Error detection
    // -----------------------------------------------------------------------

    detectError(output: string): boolean {
        return ERROR_PATTERNS.some((p) => p.test(output));
    }

    // -----------------------------------------------------------------------
    // Miscellaneous
    // -----------------------------------------------------------------------

    async promptManualInput(): Promise<string | null> {
        const result = await vscode.window.showInputBox({
            prompt: '에러 로그를 붙여넣으세요 (긴 내용은 Ctrl+V)',
            placeHolder: 'Error: ...',
            ignoreFocusOut: true,
        });
        return result ?? null;
    }

    // -----------------------------------------------------------------------
    // Private helpers
    // -----------------------------------------------------------------------

    private getLastLines(terminalId: string, lines: number): string {
        const buf = this.outputBuffer.get(terminalId) ?? [];
        const allText = buf.join('');
        const allLines = allText.split('\n');
        return allLines.slice(-lines).join('\n');
    }

    /**
     * Return a stable string ID for a terminal instance.
     *
     * Using a WeakMap-backed counter instead of terminal.processId because
     * processId is Thenable<number|undefined> (async), not a number — direct
     * access returns a Promise object that serialises as "[object Promise]",
     * making every terminal share the same key.
     */
    private getTerminalId(terminal: vscode.Terminal): string {
        if (!this.terminalIds.has(terminal)) {
            this.terminalIds.set(terminal, `t${++this.terminalIdCounter}`);
        }
        return this.terminalIds.get(terminal)!;
    }
}
