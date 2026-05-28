/**
 * ReCoder VSCode Extension — Entry Point (설계서 v6.4 §4.1)
 *
 * Activation:
 *   - Registers the Sidebar WebviewViewProvider (sidebar/SidebarProvider — primary v6.4 골격).
 *   - Registers all commands (analyzeError, runWithRecoder, generateDockerfile, etc.).
 *   - Manages CoreManager lifecycle (lazy spawn on first sidebar open / command).
 *   - Shell Integration 리스너 (§8.1 — primary output collection).
 *   - Deactivation triggers graceful Core shutdown.
 *
 * Lazy Spawn: Sidebar 첫 열기 또는 명령 최초 실행 시 Core 시작 (§6.1).
 */

import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { CoreManager } from './core/CoreManager';
import { ApiClient } from './core/ApiClient';
import { PollingService } from './core/PollingService';
import { SidebarProvider } from './sidebar/SidebarProvider';
import { WorkbenchPanel } from './sidebar/WorkbenchPanel';
import { TerminalCollector } from './terminal/TerminalCollector';
import { AnalyzeRequest } from './types';
import { BridgeClient } from './bridge/BridgeClient';

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

    // ── Shell Integration listeners (§8.1 — primary output collection) ────────
    // registerShellIntegrationListeners wires up onDidEndTerminalShellExecution
    // so that terminal output actually flows into the buffer used by
    // getLatestOutput().  Must be called before any command is registered.
    terminalCollector.registerShellIntegrationListeners(
        context,
        (output: import('./types').TerminalOutput) => {
            // Auto-analysis: only trigger when an error pattern is detected.
            // This is the §8.1 "passive monitoring" path.
            if (!terminalCollector.detectError(output.output)) { return; }
            const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
            const request: AnalyzeRequest = {
                workspace_path: workspacePath,
                terminal_output: output.output,
                command: output.command,
                project_files_summary: buildProjectFilesSummary(workspacePath),
            };
            sidebarProvider.triggerAnalysis(request);
            void vscode.commands.executeCommand('recoder.sidebarView.focus');
        }
    );

    // Attach to any terminal that gains Shell Integration support at runtime.
    // onDidChangeTerminalShellIntegration became stable in VSCode 1.93.
    // Use try/catch guard so activation succeeds on older versions too.
    try {
        context.subscriptions.push(
            vscode.window.onDidChangeTerminalShellIntegration(({ terminal, shellIntegration }) => {
                terminalCollector.attachShellIntegration(terminal, shellIntegration);
            })
        );
    } catch (_e) {
        // API not available on this VSCode version — Shell Integration auto-attach disabled.
    }

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

    // ── Command: Start Core ─────────────────────────────────────────────────
    // 명시적으로 Core 만 시작하고 싶을 때 (사이드바를 열지 않고도 사용 가능).
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.startCore', async () => {
            await ensureCoreRunning(coreManager, sidebarProvider);
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

    // ── Command: Open Workbench ─────────────────────────────────────────────
    // 사이드바의 "Workbench 열기" 버튼이나 명령 팔레트에서 호출.
    // Editor Area 에 풀스크린 탭으로 열린다 — 4탭 펼침, 넓은 작업 공간.
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.openWorkbench', async () => {
            await ensureCoreRunning(coreManager, sidebarProvider);
            WorkbenchPanel.createOrShow(
                context.extensionUri,
                apiClient,
                coreManager,
                pollingService,
            );
        })
    );

    // ── Commands: Sidebar Location (Kiro-style 우측 / 기본 좌측) ─────────────
    // VSCode 의 view container 위치 이동은 사용자 인터랙션 컨텍스트에서만
    // 동작하는 비공식 명령 (workbench.action.moveView*) 을 사용한다. 안되면
    // 사용자에게 수동 가이드 알림을 띄운다.
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.moveToRightSidebar', async () => {
            await moveRecoderTo('right', context);
        }),
        vscode.commands.registerCommand('recoder.moveToLeftSidebar', async () => {
            await moveRecoderTo('left', context);
        })
    );

    // ── First-run: Kiro 스타일로 우측 사이드바 자동 배치 ─────────────────────
    // 첫 활성화에서만 1회 실행. 사용자가 의도적으로 좌측으로 옮긴 후에는 다시
    // 강제로 옮기지 않는다.
    const KEY_LAYOUT_INITIALIZED = 'recoder.layout.initializedV1';
    if (!context.globalState.get<boolean>(KEY_LAYOUT_INITIALIZED, false)) {
        setTimeout(async () => {
            try {
                await moveRecoderTo('right', context, /*silent=*/ true);
            } catch {
                // ignore
            } finally {
                await context.globalState.update(KEY_LAYOUT_INITIALIZED, true);
            }
        }, 1500);
    }

    // ── Terminal data listener ──────────────────────────────────────────────
    // onDidWriteTerminalData is a VSCode proposed API (terminalDataWriteEvent)
    // and cannot be used without --enable-proposed-api in production builds.
    // Primary output collection is handled by TerminalShellIntegration above
    // (§8.1 Shell Integration 우선 수집). Raw data capture via proposed API
    // is a 2학기 optional enhancement.

    // ── ReCoder Bridge (Discord 봇 → 실시간 코드 삽입) ────────────────────────
    // recoder.bridge.enabled 설정이 true 이면 봇의 WebSocket에 접속하고,
    // 봇이 푸시하는 스트리밍 코드 청크를 활성 워크스페이스 파일에 실시간 삽입한다.
    const bridgeCfg = vscode.workspace.getConfiguration('recoder.bridge');
    if (bridgeCfg.get<boolean>('enabled', false)) {
        const url = bridgeCfg.get<string>('url', 'ws://127.0.0.1:7780/ws');
        const token = bridgeCfg.get<string>('token', '');
        const bridge = new BridgeClient(url, token);
        bridge.start();
        context.subscriptions.push(bridge);
    }

    console.log('[ReCoder] Extension activated.');
}

/**
 * ReCoder view container 를 좌/우 사이드바로 이동.
 *
 * VSCode 의 view 이동 명령들은 활성 view 컨텍스트를 요구하므로,
 *   1) 먼저 ReCoder 사이드바에 포커스를 준 다음
 *   2) workbench.action.moveView* 명령을 실행한다.
 *
 * 명령이 실패하거나 없는 경우 사용자에게 수동 가이드를 표시한다 (silent=false 일 때만).
 */
async function moveRecoderTo(
    side: 'left' | 'right',
    context: vscode.ExtensionContext,
    silent: boolean = false,
): Promise<void> {
    try {
        // ReCoder view 에 포커스 (이게 있어야 활성 view 컨텍스트 잡힘)
        await vscode.commands.executeCommand('recoder.sidebarView.focus');
        await new Promise(r => setTimeout(r, 300));

        if (side === 'right') {
            // Secondary Side Bar 활성화 (안 보이면 토글로 보이게)
            try {
                await vscode.commands.executeCommand('workbench.action.focusAuxiliaryBar');
            } catch {
                // best-effort
            }
            // 현재 활성 view 를 secondary side bar 로 이동
            await vscode.commands.executeCommand('workbench.action.moveViewToSecondarySideBar');
        } else {
            // 기본 좌측 Activity Bar 로 복귀
            await vscode.commands.executeCommand('workbench.action.moveViewToActivityBar');
        }

        if (!silent) {
            const target = side === 'right' ? '오른쪽' : '왼쪽';
            vscode.window.showInformationMessage(`ReCoder를 ${target} 사이드바로 이동했습니다.`);
        }
    } catch (err) {
        if (silent) {
            return; // first-run에는 조용히 실패
        }

        const sideLabel = side === 'right' ? 'Secondary Side Bar (오른쪽)' : 'Activity Bar (왼쪽)';
        const choice = await vscode.window.showInformationMessage(
            `자동 이동이 실패했습니다. Activity Bar 의 ReCoder 아이콘을 우클릭해서 "Move View Container to ${sideLabel}" 를 선택해주세요.`,
            '알겠음',
        );
        void choice; // suppress unused-var lint
        console.warn('[ReCoder] moveRecoderTo failed:', err);
    }
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
