/**
 * ReCoder VSCode Extension -- Entry Point
 * 3-area: Sidebar (mini) + Workbench (4-tab) + Panel
 */
import * as vscode from 'vscode';
import { CoreManager } from './core/coreManager';
import { SidebarProvider } from './ui/sidebarProvider';
import { WorkbenchPanel } from './ui/workbenchPanel';
import { TerminalCollector } from './collectors/terminalCollector';

let coreManager: CoreManager;
let sidebarProvider: SidebarProvider;
let terminalCollector: TerminalCollector;

export async function activate(context: vscode.ExtensionContext) {
    coreManager = new CoreManager(context);
    terminalCollector = new TerminalCollector(coreManager);
    sidebarProvider = new SidebarProvider(context, coreManager, terminalCollector);

    terminalCollector.setSidebarCallback((proposal) => {
        sidebarProvider.sendMessage({ type: 'auto_detected' });
        sidebarProvider.sendMessage({ type: 'analyze_result', data: proposal });
    });

    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('recoder-sidebar', sidebarProvider)
    );

    function openWorkbench() {
        const panel = WorkbenchPanel.createOrShow(
            context,
            async (msg) => {
                await sidebarProvider.handleWorkbenchMessage(msg);
            }
        );
        sidebarProvider.setWorkbenchSendFn((m) => panel.sendMessage(m));
    }

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
        }),
        vscode.commands.registerCommand('recoder.openDashboard', async () => {
            await coreManager.ensureRunning();
            const url = coreManager.getDashboardUrl();
            if (!url) {
                vscode.window.showWarningMessage('ReCoder: Dashboard URL not found. Check Core status.');
                return;
            }
            await vscode.env.openExternal(vscode.Uri.parse(url));
        }),
        vscode.commands.registerCommand('recoder.openWorkbench', () => openWorkbench())
    );

    terminalCollector.register(context);
}

export function deactivate() {
    coreManager?.stop();
    terminalCollector?.dispose();
}
