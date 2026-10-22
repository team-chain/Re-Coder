/**
 * 빠른 이동 (2.0.44) — VS Code 쪽 연결: Alt+Q 휠 명령, 기능별 열기 명령, 설정 읽기·쓰기.
 * 설정 해석 규칙은 quickAccess.ts(순수 모듈)에 있다. 휠 자체는 ReCoder 창(웹뷰)이 그린다.
 */
import * as vscode from 'vscode';
import {
    QUICK_SLOTS, QUICK_TARGETS, QuickSlot, QuickTarget, SLOT_COMMAND, TARGET_BY_ID,
    resolveQuickAccess, slotKeyLabel, toSetting, wheelKeyLabel, withSlot,
} from './quickAccess';

const SECTION = 'recoder';
const KEY = 'quickAccess';
const IS_MAC = process.platform === 'darwin';

export interface QuickAccessPayload {
    slots: Record<QuickSlot, string>;
    problems: string[];
    targets: Array<{ id: string; label: string }>;
    keys: Record<QuickSlot, string>;
    wheelKey: string;
}

export function currentQuickAccess(): QuickAccessPayload {
    const { slots, problems } = resolveQuickAccess(vscode.workspace.getConfiguration(SECTION).get(KEY));
    const keys = {} as Record<QuickSlot, string>;
    for (const s of QUICK_SLOTS) keys[s] = slotKeyLabel(s);
    return { slots, problems, targets: QUICK_TARGETS.map((t) => ({ id: t.id, label: t.label })), keys, wheelKey: wheelKeyLabel(IS_MAC) };
}

/**
 * 8칸을 저장한다. 작업 영역 설정이 이 값을 덮고 있으면 그쪽에 쓴다 —
 * 사용자 설정에만 쓰면 휠에서 바꿔도 아무 일도 일어나지 않는 것처럼 보인다.
 */
export async function saveQuickAccess(slots: Record<QuickSlot, string> | null): Promise<void> {
    const config = vscode.workspace.getConfiguration(SECTION);
    const info = config.inspect(KEY);
    const target = info?.workspaceFolderValue !== undefined ? vscode.ConfigurationTarget.WorkspaceFolder
        : info?.workspaceValue !== undefined ? vscode.ConfigurationTarget.Workspace
            : vscode.ConfigurationTarget.Global;
    await config.update(KEY, slots ? toSetting(slots) : undefined, target);
}

export async function setQuickSlot(slot: string, targetId: string): Promise<boolean> {
    const before = currentQuickAccess().slots;
    const next = withSlot(before, slot, targetId);
    if (next === before) return false;
    await saveQuickAccess(next);
    return true;
}

// ── 휠을 닫았을 때 원래 편집기로 돌아가기 ─────────────────────────────────
//: 코드를 치다가 Alt+Q → Esc 했으면 ReCoder 창이 아니라 치던 파일·커서로 돌아가야 한다.
interface ReturnPoint { uri: vscode.Uri; column?: vscode.ViewColumn; selection: vscode.Selection; }
let returnPoint: ReturnPoint | null = null;

export function rememberReturnPoint(): void {
    const ed = vscode.window.activeTextEditor;
    returnPoint = ed ? { uri: ed.document.uri, column: ed.viewColumn, selection: ed.selection } : null;
}

/** 휠이 닫혔다. 고르지 않고 닫았으면(Esc) 원래 편집기로 돌아간다. */
export async function quickWheelClosed(cancelled: boolean): Promise<void> {
    const back = returnPoint;
    returnPoint = null;
    if (!cancelled || !back) return;
    try {
        const doc = await vscode.workspace.openTextDocument(back.uri);
        await vscode.window.showTextDocument(doc, { viewColumn: back.column, selection: back.selection, preserveFocus: false });
    } catch {
        //: 그 사이 파일이 지워졌거나 닫을 수 없는 문서 — 돌아갈 곳이 없으면 그대로 둔다.
    }
}

export interface QuickAccessDeps {
    /** 기능 화면을 연다(ReCoder 창이 닫혀 있으면 열고 그 화면으로 시작). */
    open: (target: QuickTarget) => void;
    /** ReCoder 창에 휠을 띄운다. mode "edit" 면 바로 칸 바꾸기. */
    showWheel: (mode: 'pick' | 'edit') => void;
    /** 설정이 바뀌면 열린 ReCoder 화면들에 알린다. */
    broadcast: (state: QuickAccessPayload) => void;
}

export function registerQuickAccess(context: vscode.ExtensionContext, deps: QuickAccessDeps): void {
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.quickAccess.wheel', () => {
            rememberReturnPoint();
            deps.showWheel('pick');
        }),
        vscode.commands.registerCommand('recoder.quickAccess.configure', () => {
            returnPoint = null;
            deps.showWheel('edit');
        }),
    );
    //: 칸마다 명령도 둔다 — 휠 없이 바로 가는 키를 쓰고 싶은 사람은 VS Code 단축키 설정에서 직접 붙인다(기본 키 없음).
    for (const slot of QUICK_SLOTS) {
        context.subscriptions.push(vscode.commands.registerCommand(`recoder.quick.${SLOT_COMMAND[slot]}`, async () => {
            const id = currentQuickAccess().slots[slot];
            const target = id ? TARGET_BY_ID.get(id) : undefined;
            if (target) { deps.open(target); return; }
            const pick = await vscode.window.showInformationMessage(
                `빠른 이동 ${slotKeyLabel(slot)} 칸에 지정된 기능이 없습니다.`, '지정하기');
            if (pick) deps.showWheel('edit');
        }));
    }
    for (const target of QUICK_TARGETS) {
        context.subscriptions.push(vscode.commands.registerCommand(`recoder.open.${target.id}`, () => deps.open(target)));
    }
    context.subscriptions.push(vscode.workspace.onDidChangeConfiguration((e) => {
        if (e.affectsConfiguration(`${SECTION}.${KEY}`)) deps.broadcast(currentQuickAccess());
    }));
}
