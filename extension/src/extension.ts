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
import { WorkbenchSidebarProvider } from './sidebar/WorkbenchSidebarProvider';
import { ReCoderPanel } from './sidebar/ReCoderPanel';
import { TerminalCollector } from './terminal/TerminalCollector';
import { AnalyzeRequest } from './types';
import { BridgeClient } from './bridge/BridgeClient';
import { ensureEnrolled, runEnrollCommand } from './gateway/enroll';

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

    // ── Discord bridge client (ws://127.0.0.1:7780/ws) ──────────────────────
    // 봇의 /make 채널에서 생성된 코드를 받아 워크스페이스에 자동 저장.
    // **활성 클라이언트를 가변 홀더로 추적한다.** const 로 잡고 재연결 때마다
    // 새 인스턴스를 만들면, dispose 는 항상 최초 인스턴스만 정리하고 이전
    // 클라이언트들이 살아남는다 — 봇 스트림 하나를 여러 클라이언트가 각각
    // 처리해 같은 문서에 중복 편집·중복 실행이 일어난다.
    let activeBridge = new BridgeClient(context);
    context.subscriptions.push(activeBridge);
    activeBridge.connect();

    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.bridge.reconnect', () => {
            activeBridge.dispose();
            activeBridge = new BridgeClient(context);
            context.subscriptions.push(activeBridge);
            activeBridge.connect();
            vscode.window.showInformationMessage('ReCoder Bridge 재연결 시도');
        }),
    );

    // ── 게이트웨이 자가발급(enroll) ───────────────────────────────────────────
    // recoder.gateway.url 이 설정돼 있고 아직 토큰이 없으면 최초 실행 시 반 코드를
    // 물어 발급한다("확장만 설치 → 반 코드 1회 → AWS 키 없이 AI"). 명령으로도 가능.
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.enroll', () => runEnrollCommand(context)),
    );
    // 자동 팝업 제거 — 운영자/일반 사용자에게 매번 'enroll 코드' 묻지 않는다.
    // 게이트웨이 연결이 필요하면 명령 팔레트에서 'recoder.enroll' 로 수동 실행.
    // void ensureEnrolled(context);

    // ── Sidebar provider ────────────────────────────────────────────────────
    const sidebarProvider = new SidebarProvider(
        context.extensionUri,
        apiClient,
        coreManager,
        pollingService,
        () => { void vscode.commands.executeCommand('recoder.openReCoder'); },
    );

    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            SidebarProvider.viewType,
            sidebarProvider,
            { webviewOptions: { retainContextWhenHidden: true } }
        )
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.generateInFolder', async (uri?: vscode.Uri) => {
            let folder = '';
            if (uri) {
                const rel = vscode.workspace.asRelativePath(uri, false);
                folder = rel === uri.fsPath ? '' : rel;
            }
            await vscode.commands.executeCommand('recoder.sidebarView.focus');
            sidebarProvider.setCodeTargetFolder(folder);
        }),
    );

    // ── Workbench sidebar view (옵션 B — 사이드바 워크벤치 대시보드) ───────────
    const workbenchSidebarProvider = new WorkbenchSidebarProvider(
        context.extensionUri,
        apiClient,
        coreManager,
        pollingService,
    );
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            WorkbenchSidebarProvider.viewType,
            workbenchSidebarProvider,
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

    // ── Command: Generate GitHub Actions Workflow ───────────────────────────
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.generateGithubActions', async () => {
            await ensureCoreRunning(coreManager, sidebarProvider);
            sidebarProvider.switchToShipMode();
            sidebarProvider.triggerGithubActionsGeneration();
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

    // ── Command: GitHub Login (VS Code native OAuth) ────────────────────────
    // VS Code 가 내장한 GitHub 인증 공급자를 사용. 토큰을 직접 받아 Core 에 전달.
    // 사용자는 별도 PAT 발급 없이 브라우저 1-click 으로 인증 가능.
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.githubLogin', async () => {
            await ensureCoreRunning(coreManager, sidebarProvider);
            try {
                const session = await vscode.authentication.getSession(
                    'github',
                    ['repo', 'workflow', 'admin:repo_hook'],
                    { createIfNone: true },
                );
                if (!session?.accessToken) {
                    vscode.window.showWarningMessage('GitHub 인증이 취소되었습니다.');
                    return;
                }
                const result = await apiClient.setGithubToken(session.accessToken);
                if (result.status === 'ok') {
                    vscode.window.showInformationMessage(
                        `GitHub 연결 완료: ${result.user ?? session.account.label}`,
                    );
                    // 진단 갱신 — github_ready chip 즉시 반영
                    sidebarProvider.triggerDiagnostics();
                } else {
                    vscode.window.showErrorMessage(
                        `GitHub 토큰 등록 실패: ${result.message ?? 'unknown error'}`,
                    );
                }
            } catch (err) {
                vscode.window.showErrorMessage(
                    `GitHub 로그인 실패: ${err instanceof Error ? err.message : String(err)}`,
                );
            }
        })
    );

    // ── Command: GitHub Logout ──────────────────────────────────────────────
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.githubLogout', async () => {
            await ensureCoreRunning(coreManager, sidebarProvider);
            const result = await apiClient.githubLogout();
            vscode.window.showInformationMessage(
                result.status === 'ok'
                    ? 'GitHub 로그아웃 완료'
                    : `로그아웃 실패: ${result.message ?? 'unknown'}`,
            );
            sidebarProvider.triggerDiagnostics();
        })
    );

    // ── Command: AWS Credentials Configure (native input boxes) ─────────────
    // showInputBox 3단계로 자격증명 수집 → Core 가 STS GetCallerIdentity 로 검증.
    // 검증을 통과한 키만 VS Code SecretStorage(OS 보안 저장소)에 보관한다.
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.awsConfigure', async () => {
            await ensureCoreRunning(coreManager, sidebarProvider);

            const accessKey = await vscode.window.showInputBox({
                title: 'AWS 자격증명 (1/3)',
                prompt: 'AWS Access Key ID',
                placeHolder: 'AKIA...',
                ignoreFocusOut: true,
                validateInput: (v) => (v && v.trim().length >= 16 ? null : 'Access Key 가 너무 짧습니다.'),
            });
            if (!accessKey) { return; }

            const secret = await vscode.window.showInputBox({
                title: 'AWS 자격증명 (2/3)',
                prompt: 'AWS Secret Access Key',
                password: true,
                ignoreFocusOut: true,
                validateInput: (v) => (v && v.trim().length >= 16 ? null : 'Secret Access Key 가 너무 짧습니다.'),
            });
            if (!secret) { return; }

            const region = await vscode.window.showInputBox({
                title: 'AWS 자격증명 (3/3)',
                prompt: 'AWS Region',
                value: 'ap-northeast-2',
                placeHolder: 'ap-northeast-2',
                ignoreFocusOut: true,
                validateInput: (v) => (v && /^[a-z]{2}-[a-z]+-\d+$/.test(v.trim()) ? null : '예: ap-northeast-2'),
            });
            if (!region) { return; }

            await vscode.window.withProgress(
                { location: vscode.ProgressLocation.Notification, title: 'AWS 자격증명 검증 중…' },
                async () => {
                    const result = await apiClient.connectAws({
                        accessKeyId: accessKey.trim(),
                        secretAccessKey: secret.trim(),
                        region: region.trim(),
                    });
                    if (result.ready) {
                        await coreManager.storeAwsCredentials({
                            accessKeyId: accessKey.trim(),
                            secretAccessKey: secret.trim(),
                            region: region.trim(),
                        });
                        // connectAws는 검증을 통과한 자격증명을 현재 Core 메모리에
                        // 적용한다. 성공 알림만 띄우면 배포 진단은 연결 전 캐시를 계속
                        // 보여줄 수 있으므로, 즉시 다시 실행해 AWS Deploy Ready를 갱신한다.
                        sidebarProvider.triggerDiagnostics();
                        const acct = result.identity?.account ?? 'unknown';
                        vscode.window.showInformationMessage(
                            `AWS 연결 완료 (account ${acct}, region ${region.trim()})`,
                        );
                        sidebarProvider.triggerDiagnostics();
                    } else {
                        vscode.window.showErrorMessage(
                            `AWS 자격증명 검증 실패: ${result.message ?? 'STS 호출 실패'}`,
                        );
                    }
                },
            );
        })
    );

    // ── Command: AWS Credentials Clear ──────────────────────────────────────
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.awsClear', async () => {
            const confirm = await vscode.window.showWarningMessage(
                'VS Code 보안 금고에 저장된 AWS 자격증명을 삭제하시겠습니까?',
                { modal: true },
                '삭제',
            );
            if (confirm !== '삭제') { return; }
            await coreManager.clearAwsCredentials();
            await coreManager.restart();
            vscode.window.showInformationMessage('AWS 자격증명 삭제 완료');
            sidebarProvider.triggerDiagnostics();
        })
    );

    // ── Command: Setup Wizard (모든 외부 연결을 한 번에) ────────────────────
    // 진단 결과를 보고, 비어있는 항목만 차례로 설정 다이얼로그를 띄운다.
    // 명령 팔레트의 "ReCoder: Setup Wizard" 진입점.
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.setupWizard', async () => {
            await ensureCoreRunning(coreManager, sidebarProvider);

            // 1) 현재 상태 가져오기
            const [gh, aws] = await Promise.all([
                apiClient.getGithubStatus(true).catch(() => ({ status: 'error' as const })),
                apiClient.getAwsStatus().catch(() => ({ ready: false } as { ready: boolean })),
            ]);

            const needsGithub = gh.status !== 'authenticated';
            const needsAws = !aws.ready;

            if (!needsGithub && !needsAws) {
                vscode.window.showInformationMessage('이미 GitHub & AWS 모두 연결되어 있습니다.');
                return;
            }

            const targets: string[] = [];
            if (needsGithub) { targets.push('GitHub'); }
            if (needsAws) { targets.push('AWS'); }
            const start = await vscode.window.showInformationMessage(
                `다음 항목을 설정합니다: ${targets.join(', ')}`,
                { modal: true },
                '시작',
            );
            if (start !== '시작') { return; }

            if (needsGithub) {
                await vscode.commands.executeCommand('recoder.githubLogin');
            }
            if (needsAws) {
                await vscode.commands.executeCommand('recoder.awsConfigure');
            }

            vscode.window.showInformationMessage('초기 설정 완료. 사이드바의 ready chip 을 확인하세요.');
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

    // ── Command: Open ReCoder workspace ─────────────────────────────────────
    // 사이드바의 "Workbench 열기" 버튼이나 명령 팔레트에서 호출.
    // Editor Area 에 왼쪽 작업 영역 + 오른쪽 AI 대화가 한 화면으로 열린다.
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.openWorkbench', async () => {
            ReCoderPanel.createOrShow(context.extensionUri, sidebarProvider);
            void ensureCoreRunning(coreManager, sidebarProvider);
        }),
        vscode.commands.registerCommand('recoder.openReCoder', async () => {
            ReCoderPanel.createOrShow(context.extensionUri, sidebarProvider);
            void ensureCoreRunning(coreManager, sidebarProvider);
        }),
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
        sidebarProvider.triggerDiagnostics();
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
