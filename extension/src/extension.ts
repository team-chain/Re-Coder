/**
 * ReCoder VSCode Extension — Entry Point
 *
 * Activation:
 *   - Registers the Sidebar WebviewViewProvider.
 *   - Registers all commands (analyzeError, runWithRecoder, generateDockerfile, etc.).
 *   - Manages CoreManager lifecycle (lazy spawn on first sidebar open / command).
 *   - Deactivation triggers graceful Core shutdown.
 */

import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { CoreManager } from './core/CoreManager';
import { ApiClient } from './core/ApiClient';
import { PollingService } from './core/PollingService';
import { SidebarProvider } from './sidebar/SidebarProvider';
import { TerminalCollector } from './terminal/TerminalCollector';
import { AnalyzeRequest } from './types';

// ---------------------------------------------------------------------------
// Activate
// ---------------------------------------------------------------------------

export function activate(context: vscode.ExtensionContext): void {
    console.log('[ReCoder] Extension activating…');

    // ── Core service instances ──────────────────────────────────────────────
    const coreManager = CoreManager.getInstance(context);
    const apiClient = new ApiClient(coreManager);
    const pollingService = new PollingService(coreManager, apiClient);
    const terminalCollector = new TerminalCollector(apiClient);

    // ── Sidebar provider ────────────────────────────────────────────────────
    const sidebarProvider = new SidebarProvider(
        context.extensionUri,
        apiClient,
        coreManager,
        pollingService,
    );

    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            SidebarProvider.viewType,
            sidebarProvider,
            { webviewOptions: { retainContextWhenHidden: true } }
        )
    );

    // ── Shell Integration listener ──────────────────────────────────────────
    // Attach to any terminal that gains Shell Integration support at runtime.
    context.subscriptions.push(
        vscode.window.onDidChangeTerminalShellIntegration(
            async ({ terminal, shellIntegration }) => {
                terminalCollector.attachShellIntegration(terminal, shellIntegration);
            }
        )
    );

    // Attach to terminals already open at activation time
    for (const terminal of vscode.window.terminals) {
        if (terminal.shellIntegration) {
            terminalCollector.attachShellIntegration(terminal, terminal.shellIntegration);
        }
    }

    // ── Command: Analyze Error ──────────────────────────────────────────────
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.analyzeError', async () => {
            await ensureCoreRunning(coreManager, sidebarProvider);

            const editor = vscode.window.activeTextEditor;
            const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';

            const request: AnalyzeRequest = {
                workspace_path: workspacePath,
                active_file_path: editor?.document.uri.fsPath,
                selected_text: editor?.document.getText(editor.selection) || undefined,
                terminal_output: terminalCollector.getLatestOutput(),
                project_files_summary: buildProjectFilesSummary(workspacePath),
            };

            sidebarProvider.triggerAnalysis(request);
            await vscode.commands.executeCommand('recoder.sidebarView.focus');
        })
    );

    // ── Command: Run with ReCoder ───────────────────────────────────────────
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.runWithRecoder', async () => {
            await ensureCoreRunning(coreManager, sidebarProvider);

            const command = await vscode.window.showInputBox({
                prompt: 'Enter command to run with ReCoder monitoring',
                placeHolder: 'e.g. python main.py',
                value: getDefaultRunCommand(vscode.workspace.workspaceFolders?.[0]?.uri.fsPath),
            });

            if (!command) {
                return;
            }

            terminalCollector.createReCoderTerminal(command).then((output) => {
                if (output.output) {
                    const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
                    const request: AnalyzeRequest = {
                        workspace_path: workspacePath,
                        active_file_path: vscode.window.activeTextEditor?.document.uri.fsPath,
                        terminal_output: output.output,
                        command: output.command,
                        project_files_summary: buildProjectFilesSummary(workspacePath),
                    };
                    sidebarProvider.triggerAnalysis(request);
                }
            }).catch(console.error);

            await vscode.commands.executeCommand('recoder.sidebarView.focus');
        })
    );

    // ── Command: Generate Dockerfile ────────────────────────────────────────
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.generateDockerfile', async () => {
            await ensureCoreRunning(coreManager, sidebarProvider);
            sidebarProvider.switchToShipMode();
            sidebarProvider.triggerDockerfileGeneration();
            await vscode.commands.executeCommand('recoder.sidebarView.focus');
        })
    );

    // ── Command: Run Diagnostics ────────────────────────────────────────────
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.runDiagnostics', async () => {
            await ensureCoreRunning(coreManager, sidebarProvider);
            sidebarProvider.triggerDiagnostics();
            await vscode.commands.executeCommand('recoder.sidebarView.focus');
        })
    );

    // ── Command: Restart Core ───────────────────────────────────────────────
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.restartCore', async () => {
            await vscode.window.withProgress(
                { location: vscode.ProgressLocation.Notification, title: 'Restarting ReCoder Core…' },
                async () => {
                    await coreManager.shutdown(true);
                    await coreManager.ensureRunning();
                    sidebarProvider.postMessage('core.restarted', {});
                }
            );
        })
    );

    // ── Command: Stop Core ──────────────────────────────────────────────────
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.stopCore', async () => {
            await coreManager.shutdown();
            sidebarProvider.postMessage('core.stopped', {});
            vscode.window.showInformationMessage('ReCoder Core stopped.');
        })
    );

    // ── Terminal data listener (Shell Integration fallback) ─────────────────
    context.subscriptions.push(
        (vscode.window as any).onDidWriteTerminalData?.(({ terminal, data }: { terminal: vscode.Terminal; data: string }) => {
            terminalCollector.onTerminalData(terminal, data);
        })
    );

    console.log('[ReCoder] Extension activated.');
}

// ---------------------------------------------------------------------------
// Deactivate
// ---------------------------------------------------------------------------

export async function deactivate(): Promise<void> {
    console.log('[ReCoder] Extension deactivating…');
    try {
        const coreManager = CoreManager.getInstance();
        await coreManager.shutdown();
    } catch {
        // Already stopped or not started
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function ensureCoreRunning(
    coreManager: CoreManager,
    sidebarProvider: SidebarProvider,
): Promise<void> {
    try {
        await coreManager.ensureRunning();
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        vscode.window.showErrorMessage(`ReCoder Core 시작 실패: ${msg}`);
        sidebarProvider.postMessage('core.error', { message: msg });
    }
}

function buildProjectFilesSummary(workspacePath: string): string {
    if (!workspacePath) {
        return '';
    }
    try {
        const entries = fs.readdirSync(workspacePath, { withFileTypes: true });
        const lines = entries
            .slice(0, 30)
            .map((e) => (e.isDirectory() ? `[dir] ${e.name}` : `      ${e.name}`));
        return lines.join('\n');
    } catch {
        return '';
    }
}

function getDefaultRunCommand(workspacePath?: string): string {
    if (!workspacePath) {
        return '';
    }
    if (fs.existsSync(path.join(workspacePath, 'requirements.txt'))) {
        return 'python main.py';
    }
    if (fs.existsSync(path.join(workspacePath, 'package.json'))) {
        return 'npm start';
    }
    if (fs.existsSync(path.join(workspacePath, 'go.mod'))) {
        return 'go run .';
    }
    return '';
}
