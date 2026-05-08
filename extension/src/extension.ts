/**
 * ReCoder VSCode Extension — Entry Point (설계서 v6.4 §4.1)
 * - Lazy Spawn: Sidebar 첫 열기 또는 명령 최초 실행 시 Core 시작
 * - §6.1 기준
 */
import * as vscode from 'vscode';
import { CoreManager } from './core/coreManager';
import { SidebarProvider } from './ui/sidebarProvider';
import { TerminalCollector } from './collectors/terminalCollector';

let coreManager: CoreManager;
let sidebarProvider: SidebarProvider;
let terminalCollector: TerminalCollector;

export async function activate(context: vscode.ExtensionContext) {
    coreManager = new CoreManager(context);
    sidebarProvider = new SidebarProvider(context, coreManager);
    terminalCollector = new TerminalCollector(coreManager);

    // Sidebar 등록
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('recoder-sidebar', sidebarProvider)
    );

    // 명령 등록
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.analyzeError', async () => {
            await coreManager.ensureRunning();
            const output = terminalCollector.getLatestOutput();
            sidebarProvider.sendMessage({ type: 'analyze_request', output });
        }),
        vscode.commands.registerCommand('recoder.generateDockerfile', async () => {
            await coreManager.ensureRunning();
            sidebarProvider.sendMessage({ type: 'generate_dockerfile' });
        }),
        vscode.commands.registerCommand('recoder.startCore', () => coreManager.ensureRunning()),
        vscode.commands.registerCommand('recoder.stopCore', () => coreManager.stop()),
        vscode.commands.registerCommand('recoder.runWithRecoder', async () => {
            await coreManager.ensureRunning();
            terminalCollector.createRecoderTerminal();
        })
    );

    // Terminal Shell Integration 이벤트 등록
    terminalCollector.register(context);
}

export function deactivate() {
    coreManager?.stop();
    terminalCollector?.dispose();
}
