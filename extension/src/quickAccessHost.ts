/**
 * 빠른 이동 (2.0.44) — VS Code 쪽 연결: Alt+Q 휠 명령, 기능별 열기 명령, 설정 읽기·쓰기.
 * 설정 해석 규칙은 quickAccess.ts(순수 모듈)에 있다. 휠 자체는 ReCoder 창(웹뷰)이 그린다.
 */
import * as vscode from 'vscode';
import {
    QUICK_SLOTS, QUICK_TARGETS, QuickSlot, QuickTarget, SLOT_COMMAND, TARGET_BY_ID,
    resolveQuickAccess, slotKeyLabel, wheelKeyLabel,
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

type Raw = Record<string, unknown>;
const asRaw = (v: unknown): Raw => (v && typeof v === 'object' && !Array.isArray(v) ? { ...(v as Raw) } : {});

/**
 * 설정 범위(사용자 → 작업 영역 → 폴더). VS Code 는 객체 설정을 범위끼리 **합쳐서** get() 으로 준다.
 * 그래서 합쳐진 값을 그대로 저장하면 사용자 설정이 작업 영역 파일로 복사된다 — 범위마다 자기 값만 다룬다.
 */
function layers(): { info: ReturnType<vscode.WorkspaceConfiguration['inspect']>; config: vscode.WorkspaceConfiguration } {
    const config = vscode.workspace.getConfiguration(SECTION);
    return { config, info: config.inspect(KEY) };
}

/** 한 칸을 바꾼다 — 그 칸을 실제로 정하고 있는 가장 좁은 범위에, 그 범위의 값만 고쳐서 쓴다. */
async function writeSlot(slot: QuickSlot, value: string): Promise<void> {
    const { config, info } = layers();
    const scopes: Array<{ target: vscode.ConfigurationTarget; value: unknown }> = [
        { target: vscode.ConfigurationTarget.WorkspaceFolder, value: info?.workspaceFolderValue },
        { target: vscode.ConfigurationTarget.Workspace, value: info?.workspaceValue },
        { target: vscode.ConfigurationTarget.Global, value: info?.globalValue },
    ];
    //: 이미 값이 있는 가장 좁은 범위(없으면 사용자 설정). 더 좁은 범위가 덮고 있으면 넓은 곳에 써도 안 보인다.
    const at = scopes.findIndex((s) => s.value !== undefined);
    const idx = at < 0 ? scopes.length - 1 : at;
    const own = asRaw(scopes[idx].value);
    //: 그 범위 아래(넓은 범위 + 기본값)가 이미 같은 값을 주면 적지 않는다 — 기본값이 바뀌면 따라가게.
    const below = resolveQuickAccess(Object.assign({}, ...scopes.slice(idx + 1).reverse().map((s) => asRaw(s.value)))).slots;
    if (below[slot] === value) delete own[slot]; else own[slot] = value;
    await config.update(KEY, Object.keys(own).length ? own : undefined, scopes[idx].target);
}

/** 기본값으로 — 값이 있는 모든 범위를 비운다(한 범위만 비우면 다른 범위 값이 남아 "안 돌아간 것"처럼 보인다). */
async function clearAll(): Promise<void> {
    const { config, info } = layers();
    if (info?.workspaceFolderValue !== undefined) await config.update(KEY, undefined, vscode.ConfigurationTarget.WorkspaceFolder);
    if (info?.workspaceValue !== undefined) await config.update(KEY, undefined, vscode.ConfigurationTarget.Workspace);
    if (info?.globalValue !== undefined) await config.update(KEY, undefined, vscode.ConfigurationTarget.Global);
}

//: 휠에서 빠르게 두 칸을 바꾸면 읽기-고치기-쓰기가 겹쳐 앞의 변경이 사라진다 — 차례로 한다.
let queue: Promise<unknown> = Promise.resolve();
function serial<T>(job: () => Promise<T>): Promise<T> {
    const run = queue.then(job, job);
    queue = run.catch(() => undefined);
    return run;
}

/** 한 칸 저장. 모르는 칸·기능이면 false. 저장 실패는 예외로 알린다(화면이 다시 맞추도록). */
export function setQuickSlot(slot: string, targetId: string): Promise<boolean> {
    return serial(async () => {
        if (!QUICK_SLOTS.includes(slot as QuickSlot)) return false;
        if (targetId !== '' && !TARGET_BY_ID.has(targetId)) return false;
        await writeSlot(slot as QuickSlot, targetId);
        return true;
    });
}

export function resetQuickAccess(): Promise<void> {
    return serial(clearAll);
}

// ── 휠을 닫았을 때 원래 편집기로 돌아가기 ─────────────────────────────────
//: 코드를 치다가 Alt+Q → Esc 했으면 ReCoder 창이 아니라 치던 파일·커서로 돌아가야 한다.
type ReturnPoint =
    | { kind: 'text'; uri: vscode.Uri; column?: vscode.ViewColumn; selection: vscode.Selection }
    | { kind: 'diff'; original: vscode.Uri; modified: vscode.Uri; label: string; column?: vscode.ViewColumn }
    | { kind: 'notebook'; uri: vscode.Uri; column?: vscode.ViewColumn };
let returnPoint: ReturnPoint | null = null;

function captureReturnPoint(): ReturnPoint | null {
    const tab = vscode.window.tabGroups?.activeTabGroup?.activeTab;
    const column = vscode.window.tabGroups?.activeTabGroup?.viewColumn;
    const input = tab?.input as { original?: vscode.Uri; modified?: vscode.Uri; uri?: vscode.Uri; notebookType?: string } | undefined;
    if (input?.original && input.modified) return { kind: 'diff', original: input.original, modified: input.modified, label: tab?.label ?? '', column };
    if (input?.uri && input.notebookType) return { kind: 'notebook', uri: input.uri, column };
    const ed = vscode.window.activeTextEditor;
    return ed ? { kind: 'text', uri: ed.document.uri, column: ed.viewColumn, selection: ed.selection } : null;
}

/**
 * Alt+Q 를 눌렀다. 편집기에서 눌렀으면 돌아갈 곳을 기억한다.
 * ReCoder 창 안에서 다시 누른 경우(휠 닫기)에는 편집기가 없으니 앞서 기억한 곳을 지우지 않는다.
 */
export function rememberReturnPoint(): void {
    const here = captureReturnPoint();
    if (here) returnPoint = here;
}

export function forgetReturnPoint(): void { returnPoint = null; }

/**
 * 휠이 닫혔다. reason:
 *  - "cancel": Esc·Alt+Q 다시 누름 — 원래 편집기로 돌아간다.
 *  - "pick": 기능을 골랐다 — 그 화면에 머문다.
 *  - "dismiss": 다른 곳을 눌러 창을 떠났다 — 사용자가 이미 간 곳에 그대로 둔다.
 */
export async function quickWheelClosed(reason: 'cancel' | 'pick' | 'dismiss'): Promise<void> {
    const back = returnPoint;
    returnPoint = null;
    if (reason !== 'cancel' || !back) return;
    try {
        if (back.kind === 'diff') {
            await vscode.commands.executeCommand('vscode.diff', back.original, back.modified, back.label, { viewColumn: back.column });
        } else if (back.kind === 'notebook') {
            const nb = await vscode.workspace.openNotebookDocument(back.uri);
            await vscode.window.showNotebookDocument(nb, { viewColumn: back.column });
        } else {
            const doc = await vscode.workspace.openTextDocument(back.uri);
            await vscode.window.showTextDocument(doc, { viewColumn: back.column, selection: back.selection, preserveFocus: false });
        }
    } catch {
        //: 그 사이 파일이 지워졌거나 열 수 없는 문서 — 돌아갈 곳이 없으면 그대로 둔다.
    }
}

// ── 터미널에서도 Alt+Q ─────────────────────────────────────────────────
//: 터미널에 포커스가 있으면 VS Code 는 키를 셸로 보낸다(셸의 Alt+Q 는 줄 밀어 두기 등).
//: terminal.integrated.commandsToSkipShell 에 휠 명령을 넣어야 VS Code 가 받는다 — 사용자 설정을 바꾸는 일이라 직접 고를 때만.
const WHEEL_COMMAND = 'recoder.quickAccess.wheel';
const SKIP_SHELL = 'commandsToSkipShell';

export function wheelWorksInTerminal(): boolean {
    const list = vscode.workspace.getConfiguration('terminal.integrated').get<unknown>(SKIP_SHELL);
    return Array.isArray(list) && list.includes(WHEEL_COMMAND) && !list.includes(`-${WHEEL_COMMAND}`);
}

/** 사용자 설정에 휠 명령을 더한다(이미 있으면 그대로). 다른 항목은 건드리지 않는다. 바꿨으면 true. */
export async function enableWheelInTerminal(): Promise<boolean> {
    const config = vscode.workspace.getConfiguration('terminal.integrated');
    const own = config.inspect<unknown>(SKIP_SHELL)?.globalValue;
    const list = Array.isArray(own) ? own.filter((x) => x !== `-${WHEEL_COMMAND}`) : [];
    if (list.includes(WHEEL_COMMAND) && Array.isArray(own) && own.length === list.length) return false;
    if (!list.includes(WHEEL_COMMAND)) list.push(WHEEL_COMMAND);
    await config.update(SKIP_SHELL, list, vscode.ConfigurationTarget.Global);
    return true;
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
            forgetReturnPoint();
            deps.showWheel('edit');
        }),
        vscode.commands.registerCommand('recoder.quickAccess.enableInTerminal', async () => {
            if (wheelWorksInTerminal()) {
                void vscode.window.showInformationMessage('이미 터미널에서도 Alt+Q 로 빠른 이동 휠이 열립니다.');
                return;
            }
            const ok = await vscode.window.showInformationMessage(
                '터미널에서 Alt+Q 를 셸 대신 ReCoder 가 받도록 사용자 설정(terminal.integrated.commandsToSkipShell)에 한 줄 더합니다. 셸의 Alt+Q 기능은 쓸 수 없게 됩니다.',
                { modal: true }, '더하기');
            if (ok !== '더하기') return;
            try {
                await enableWheelInTerminal();
                void vscode.window.showInformationMessage('이제 터미널에서도 Alt+Q 로 빠른 이동 휠이 열립니다.');
            } catch (err) {
                void vscode.window.showErrorMessage(`설정을 바꾸지 못했습니다: ${err instanceof Error ? err.message : String(err)}`);
            }
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
