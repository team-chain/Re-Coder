import * as vscode from 'vscode';

const MIGRATION_KEY = 'recoder.layout.activityBarV2';
const VIEW_IDS = ['recoder.sidebarView'] as const;

/** Keep existing view IDs/handlers, but recover views moved by the old right-sidebar setup. */
export async function migrateActivityBar(state: vscode.Memento): Promise<void> {
    if (state.get<boolean>(MIGRATION_KEY, false)) { return; }
    // New installs get the visible Activity Bar contribution without opening a tab on startup.
    if (state.get<boolean>('recoder.layout.initializedV1', false)) {
        await restoreRecoderViews();
    }
    // Respect deliberate placement choices after this one-time upgrade.
    await state.update(MIGRATION_KEY, true);
}

export async function restoreRecoderViews(): Promise<void> {
    // Per-view reset also resets an empty default container's location. The new container ID
    // avoids the old container's persisted right location. Other extensions are untouched.
    const commands = new Set(await vscode.commands.getCommands(true));
    const resets = VIEW_IDS.map(id => `${id}.resetViewLocation`);
    if (resets.some(command => !commands.has(command))) {
        throw new Error('ReCoder 뷰 위치 명령을 찾을 수 없습니다. VS Code 창을 다시 로드해주세요.');
    }
    for (const command of resets) {
        await vscode.commands.executeCommand(command);
    }
}

export async function chooseSidebarLocation(): Promise<void> {
    // Use VS Code's placement picker for this view, regardless of the currently focused view.
    await vscode.commands.executeCommand('workbench.action.moveFocusedView', 'recoder.sidebarView');
}
