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
    setWheelOpen(false);
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

// ── 휠이 떠 있는 동안 방향키는 VS Code 가 받는다 ─────────────────────────────
//: 예전에는 휠(웹뷰)이 키를 받았다. 막 띄운 웹뷰는 키보드 포커스를 못 받는 때가 많아 "한 번 클릭해야"
//: 방향키가 먹었다. 이제 휠이 떠 있는 동안 context 키를 켜고, 방향키·Esc·E 를 VS Code 단축키로 받는다 —
//: 포커스는 치던 편집기에 그대로 있고, 클릭·입력·Enter 없이 Alt+Q → 방향키 한 번이면 된다.
export const WHEEL_CONTEXT = 'recoder.quickWheelOpen';
let wheelOpen = false;
let wheelWatch: vscode.Disposable[] = [];
let wheelTimer: ReturnType<typeof setTimeout> | undefined;
//: 아무것도 고르지 않고 이 시간이 지나면 닫는다 — 방향키가 계속 휠에 묶여 있지 않게.
export const WHEEL_IDLE_MS = 8000;

export function isWheelOpen(): boolean { return wheelOpen; }

function setWheelOpen(open: boolean): void {
    if (wheelTimer) { clearTimeout(wheelTimer); wheelTimer = undefined; }
    for (const d of wheelWatch) d.dispose();
    wheelWatch = [];
    if (wheelOpen === open && !open) return;
    wheelOpen = open;
    void vscode.commands.executeCommand('setContext', WHEEL_CONTEXT, open);
}

// ── 터미널에서도 Alt+Q ─────────────────────────────────────────────────
//: 터미널에 포커스가 있으면 VS Code 는 키를 셸로 보낸다(셸의 Alt+Q 는 줄 밀어 두기 등).
//: terminal.integrated.commandsToSkipShell 에 휠 명령을 넣어야 VS Code 가 받는다 — 사용자 설정을 바꾸는 일이라 직접 고를 때만.
const WHEEL_COMMAND = 'recoder.quickAccess.wheel';
//: 휠이 떠 있을 때 방향키·Esc·E 를 받는 명령 — 터미널에서 연 경우에도 셸이 아니라 휠로 가야 한다.
export const WHEEL_KEY_COMMANDS = ['recoder.quickWheel.key', 'recoder.quickWheel.close', 'recoder.quickWheel.edit'];
const SKIP_SHELL = 'commandsToSkipShell';

export function wheelWorksInTerminal(): boolean {
    const list = vscode.workspace.getConfiguration('terminal.integrated').get<unknown>(SKIP_SHELL);
    return Array.isArray(list) && [WHEEL_COMMAND, ...WHEEL_KEY_COMMANDS].every((c) => list.includes(c) && !list.includes(`-${c}`));
}

/** 사용자 설정에 휠 명령을 더한다(이미 있으면 그대로). 다른 항목은 건드리지 않는다. 바꿨으면 true. */
export async function enableWheelInTerminal(): Promise<boolean> {
    const config = vscode.workspace.getConfiguration('terminal.integrated');
    const own = config.inspect<unknown>(SKIP_SHELL)?.globalValue;
    const ours = [WHEEL_COMMAND, ...WHEEL_KEY_COMMANDS];
    const list = Array.isArray(own) ? own.filter((x) => !ours.some((c) => x === `-${c}`)) : [];
    if (ours.every((c) => list.includes(c)) && Array.isArray(own) && own.length === list.length) return false;
    for (const c of ours) if (!list.includes(c)) list.push(c);
    await config.update(SKIP_SHELL, list, vscode.ConfigurationTarget.Global);
    return true;
}

export interface QuickAccessDeps {
    /** 기능 화면을 연다(ReCoder 창이 닫혀 있으면 열고 그 화면으로 시작). */
    open: (target: QuickTarget) => void;
    /**
     * ReCoder 창에 휠을 띄운다. pick: 키보드 포커스는 지금 자리에 둔다(방향키는 VS Code 단축키가 받음).
     * edit: 칸 바꾸기 — 창으로 포커스를 옮긴다. slot: 처음 고른 칸.
     */
    showWheel: (mode: 'pick' | 'edit', slot?: QuickSlot) => void;
    /** 확장 쪽에서 휠을 닫았다 — 화면의 휠도 닫는다. */
    hideWheel: () => void;
    /** 설정이 바뀌면 열린 ReCoder 화면들에 알린다. */
    broadcast: (state: QuickAccessPayload) => void;
}

export function registerQuickAccess(context: vscode.ExtensionContext, deps: QuickAccessDeps): void {
    const close = (reason: 'cancel' | 'pick' | 'dismiss') => {
        if (!wheelOpen) return;
        deps.hideWheel();
        void quickWheelClosed(reason);
    };
    const openPick = () => {
        rememberReturnPoint();
        setWheelOpen(true);
        deps.showWheel('pick');
        //: 휠을 띄운 뒤 사용자가 다른 일을 시작하면(글자 입력·클릭으로 커서 이동·창 떠남·시간 초과) 닫는다.
        //: 띄우는 순간 ReCoder 탭이 앞으로 오며 생기는 "활성 편집기 바뀜"은 닫는 이유가 아니다.
        wheelWatch = [
            vscode.workspace.onDidChangeTextDocument((e) => { if (e.contentChanges.length) close('dismiss'); }),
            vscode.window.onDidChangeTextEditorSelection((e) => {
                if (e.kind === vscode.TextEditorSelectionChangeKind.Mouse || e.kind === vscode.TextEditorSelectionChangeKind.Keyboard) close('dismiss');
            }),
            vscode.window.onDidChangeWindowState((w) => { if (!w.focused) close('dismiss'); }),
        ];
        wheelTimer = setTimeout(() => close('dismiss'), WHEEL_IDLE_MS);
        (wheelTimer as { unref?: () => void }).unref?.();
    };
    const openEdit = (slot?: QuickSlot) => {
        setWheelOpen(false);
        forgetReturnPoint();
        deps.showWheel('edit', slot);
    };
    context.subscriptions.push(
        vscode.commands.registerCommand('recoder.quickAccess.wheel', () => {
            //: 휠이 떠 있을 때 Alt+Q 를 또 누르면 닫는다(켜고 끄기).
            if (wheelOpen) { close('cancel'); return; }
            openPick();
        }),
        vscode.commands.registerCommand('recoder.quickAccess.configure', () => openEdit()),
        //: 휠이 떠 있을 때만 단축키가 켜진다(package.json 의 when: recoder.quickWheelOpen).
        vscode.commands.registerCommand('recoder.quickWheel.key', (arg?: unknown) => {
            if (!wheelOpen) return;
            const slot = typeof arg === 'string' ? arg : (arg as { slot?: unknown } | undefined)?.slot;
            if (!QUICK_SLOTS.includes(slot as QuickSlot)) return;
            const id = currentQuickAccess().slots[slot as QuickSlot];
            const target = id ? TARGET_BY_ID.get(id) : undefined;
            //: 빈 칸 — 그 칸을 고른 채 칸 바꾸기로(무엇을 넣을지 바로 보이게).
            if (!target) { openEdit(slot as QuickSlot); return; }
            close('pick');
            deps.open(target);
        }),
        vscode.commands.registerCommand('recoder.quickWheel.close', () => close('cancel')),
        vscode.commands.registerCommand('recoder.quickWheel.edit', () => { if (wheelOpen) openEdit(); }),
        vscode.commands.registerCommand('recoder.quickAccess.enableInTerminal', async () => {
            if (wheelWorksInTerminal()) {
                void vscode.window.showInformationMessage('이미 터미널에서도 Alt+Q 로 빠른 이동 휠이 열립니다.');
                return;
            }
            const ok = await vscode.window.showInformationMessage(
                '터미널에서 Alt+Q 와 휠의 방향키를 셸 대신 ReCoder 가 받도록 사용자 설정(terminal.integrated.commandsToSkipShell)에 더합니다. 셸의 Alt+Q 기능은 쓸 수 없게 됩니다(방향키는 휠이 떠 있을 때만).',
                { modal: true }, '더하기');
            if (ok !== '더하기') return;
            try {
                await enableWheelInTerminal();
                void vscode.window.showInformationMessage('이제 터미널에서도 Alt+Q 로 빠른 이동 휠이 열립니다.');
            } catch (err) {
                void vscode.window.showErrorMessage(`설정을 바꾸지 못했습니다: ${err instanceof Error ? err.message : String(err)}`);
            }
        }),
        { dispose: () => setWheelOpen(false) },
    );
    //: 칸마다 명령도 둔다 — 휠 없이 바로 가는 키를 쓰고 싶은 사람은 VS Code 단축키 설정에서 직접 붙인다(기본 키 없음).
    for (const slot of QUICK_SLOTS) {
        context.subscriptions.push(vscode.commands.registerCommand(`recoder.quick.${SLOT_COMMAND[slot]}`, async () => {
            const id = currentQuickAccess().slots[slot];
            const target = id ? TARGET_BY_ID.get(id) : undefined;
            if (target) { deps.open(target); return; }
            const pick = await vscode.window.showInformationMessage(
                `빠른 이동 ${slotKeyLabel(slot)} 칸에 지정된 기능이 없습니다.`, '지정하기');
            if (pick) openEdit(slot);
        }));
    }
    for (const target of QUICK_TARGETS) {
        context.subscriptions.push(vscode.commands.registerCommand(`recoder.open.${target.id}`, () => deps.open(target)));
    }
    context.subscriptions.push(vscode.workspace.onDidChangeConfiguration((e) => {
        if (e.affectsConfiguration(`${SECTION}.${KEY}`)) deps.broadcast(currentQuickAccess());
    }));
}
