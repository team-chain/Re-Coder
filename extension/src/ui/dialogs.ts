/**
 * ReCoder 화면 안 대화창(4.0.0).
 *
 * VS Code 의 확인창(modal)은 Windows 에서 운영체제 기본 팝업(옛 Windows 창)으로 뜨고, 입력·선택은
 * 화면 위쪽 명령 입력줄에 따로 떠서 ReCoder 화면과 하나로 보이지 않았다(실사용 피드백 2026-11-15).
 * 같은 질문을 지금 보이는 ReCoder 화면 안 대화창으로 띄운다. API 모양은 vscode.window 와 같다.
 *
 *  · 화면이 1.5초 안에 "띄웠다(ui.shown)" 고 답하지 않으면(화면이 닫혔거나 옛 화면) VS Code 기본 창으로 묻는다.
 *  · 비밀값 입력은 화면에 남기지 않는다(답을 보내면 화면이 입력값을 지운다). 기록·로그에 남기지 않는다.
 *  · validateInput 은 확장 쪽에서 돌려 같은 대화창에 오류를 보여 준다.
 */
import * as vscode from 'vscode';

export type DialogKind = 'confirm' | 'input' | 'pick' | 'toast';
export type ToastTone = 'info' | 'warn' | 'error';
export interface DialogRequest {
    id: string; kind: DialogKind; tone?: ToastTone; title?: string; message?: string; detail?: string;
    /** confirm·toast 버튼 / pick 선택지 */
    items?: Array<{ label: string; description?: string; detail?: string; primary?: boolean }>;
    cancelLabel?: string; value?: string; placeholder?: string; password?: boolean; error?: string; modal?: boolean;
}
interface Target { webview: vscode.Webview; visible: () => boolean; at: number }
interface Pending { request: DialogRequest; webview: vscode.Webview; shown: boolean; resolve: (answer: Answer) => void; validate?: (v: string) => unknown }
type Answer = { index?: number; value?: string; cancelled?: boolean; fallback?: boolean };

const targets: Target[] = [];
const pending = new Map<string, Pending>();
let seq = 0;
export const SHOWN_TIMEOUT_MS = 1500;

/** 대화창을 띄울 수 있는 화면을 등록한다. 가장 최근에 등록·보이는 화면을 쓴다. */
export function registerDialogWebview(webview: vscode.Webview, visible: () => boolean): vscode.Disposable {
    const target = { webview, visible, at: Date.now() + seq++ };
    targets.push(target);
    return { dispose: () => {
        const i = targets.indexOf(target); if (i >= 0) targets.splice(i, 1);
        for (const [id, p] of pending) if (p.webview === webview) { pending.delete(id); p.resolve(p.shown ? { cancelled: true } : { fallback: true }); }
    } };
}
/** 테스트용 — 등록된 화면과 대기 중인 질문을 모두 지운다. */
export function resetDialogs(): void { targets.splice(0); pending.clear(); }

function activeTarget(): Target | undefined {
    return [...targets].sort((a, b) => b.at - a.at).find(t => { try { return t.visible(); } catch { return false; } });
}

/** 화면에서 온 ui.* 메시지를 처리한다. 처리했으면 true. */
export function handleDialogMessage(message: { type?: string; payload?: unknown }): boolean {
    if (!message?.type?.startsWith('ui.')) return false;
    const p = (message.payload ?? {}) as { id?: string; index?: number; value?: string; cancelled?: boolean };
    const item = p.id ? pending.get(p.id) : undefined;
    if (!item) return true;
    if (message.type === 'ui.shown') { item.shown = true; return true; }
    if (message.type === 'ui.answer') {
        if (!p.cancelled && item.validate && typeof p.value === 'string') {
            void Promise.resolve(item.validate(p.value)).then((error) => {
                const text = typeof error === 'string' ? error : (error as { message?: string } | undefined)?.message;
                if (text) { item.request.error = text; void item.webview.postMessage({ type: 'ui.ask', payload: { ...item.request, value: p.value } }); return; }
                pending.delete(item.request.id); item.resolve({ value: p.value });
            });
            return true;
        }
        pending.delete(item.request.id);
        item.resolve({ index: typeof p.index === 'number' ? p.index : undefined, value: typeof p.value === 'string' ? p.value : undefined, cancelled: !!p.cancelled });
    }
    return true;
}

function ask(request: Omit<DialogRequest, 'id'>, validate?: (v: string) => unknown): Promise<Answer> {
    const target = activeTarget();
    if (!target) return Promise.resolve({ fallback: true });
    const id = `ui-${Date.now().toString(36)}-${(seq++).toString(36)}`;
    const full: DialogRequest = { ...request, id };
    return new Promise<Answer>((resolve) => {
        const item: Pending = { request: full, webview: target.webview, shown: false, resolve, validate };
        pending.set(id, item);
        void Promise.resolve(target.webview.postMessage({ type: 'ui.ask', payload: full })).then(ok => {
            if (ok === false && pending.get(id) === item) { pending.delete(id); resolve({ fallback: true }); }
        }, () => { if (pending.get(id) === item) { pending.delete(id); resolve({ fallback: true }); } });
        setTimeout(() => {
            if (pending.get(id) === item && !item.shown) {
                pending.delete(id); resolve({ fallback: true });
                void target.webview.postMessage({ type: 'ui.close', payload: { id } });
            }
        }, SHOWN_TIMEOUT_MS);
    });
}

type MessageItemLike = string | vscode.MessageItem;
const itemLabel = (item: MessageItemLike) => typeof item === 'string' ? item : item.title;
type MessageArgs = [ (vscode.MessageOptions | MessageItemLike)?, ...MessageItemLike[] ];

function splitMessageArgs(args: unknown[]): { options: vscode.MessageOptions; items: MessageItemLike[] } {
    const [first, ...rest] = args;
    if (first && typeof first === 'object' && !('title' in (first as object))) return { options: first as vscode.MessageOptions, items: rest as MessageItemLike[] };
    return { options: {}, items: (first === undefined ? rest : [first, ...rest]) as MessageItemLike[] };
}

async function message(tone: ToastTone, text: string, args: unknown[], native: (...a: unknown[]) => Thenable<unknown>): Promise<unknown> {
    const { options, items } = splitMessageArgs(args);
    const visibleItems = items.filter(i => typeof i === 'string' || !i.isCloseAffordance);
    const cancel = items.find(i => typeof i !== 'string' && i.isCloseAffordance);
    const modal = !!options.modal;
    const answer = await ask({
        kind: modal ? 'confirm' : 'toast', tone, message: text, detail: options.detail, modal,
        items: visibleItems.map((i, n) => ({ label: itemLabel(i), primary: n === 0 })),
        cancelLabel: cancel ? itemLabel(cancel) : modal ? '취소' : undefined,
    });
    if (answer.fallback) return native(text, ...args);
    if (answer.cancelled || answer.index === undefined) return cancel;
    return visibleItems[answer.index];
}

const nativePick = (items: unknown, options: unknown, token: unknown): Promise<unknown> =>
    Promise.resolve((vscode.window.showQuickPick as unknown as (i: unknown, o: unknown, t: unknown) => Thenable<unknown>)(items, options, token));

/** vscode.window 와 같은 모양 — 화면 안 대화창, 안 되면 VS Code 기본 창. */
export const ui = {
    showWarningMessage<T extends MessageItemLike>(text: string, ...args: MessageArgs): Thenable<T | undefined> {
        return message('warn', text, args, (...a) => (vscode.window.showWarningMessage as (...x: unknown[]) => Thenable<unknown>)(...a)) as Thenable<T | undefined>;
    },
    showInformationMessage<T extends MessageItemLike>(text: string, ...args: MessageArgs): Thenable<T | undefined> {
        return message('info', text, args, (...a) => (vscode.window.showInformationMessage as (...x: unknown[]) => Thenable<unknown>)(...a)) as Thenable<T | undefined>;
    },
    showErrorMessage<T extends MessageItemLike>(text: string, ...args: MessageArgs): Thenable<T | undefined> {
        return message('error', text, args, (...a) => (vscode.window.showErrorMessage as (...x: unknown[]) => Thenable<unknown>)(...a)) as Thenable<T | undefined>;
    },
    async showInputBox(options: vscode.InputBoxOptions = {}, token?: vscode.CancellationToken): Promise<string | undefined> {
        const answer = await ask({
            kind: 'input', title: options.title, message: options.prompt, value: options.value, placeholder: options.placeHolder,
            password: !!options.password, modal: true,
        }, options.validateInput as ((v: string) => unknown) | undefined);
        if (answer.fallback) return vscode.window.showInputBox(options, token);
        return answer.cancelled ? undefined : answer.value;
    },
    async showQuickPick<T extends vscode.QuickPickItem | string>(items: readonly T[] | Thenable<readonly T[]>, options: vscode.QuickPickOptions = {}, token?: vscode.CancellationToken): Promise<T | undefined> {
        const list = await items;
        //: 여러 개 고르기·구분선은 VS Code 기본 선택창이 더 정확하다.
        if ((options as { canPickMany?: boolean }).canPickMany || list.some(i => typeof i !== 'string' && i.kind !== undefined && i.kind === vscode.QuickPickItemKind?.Separator))
            return nativePick(list, options, token) as Promise<T | undefined>;
        const answer = await ask({
            kind: 'pick', title: options.title, message: options.placeHolder, modal: true,
            items: list.map(i => typeof i === 'string' ? { label: i } : { label: i.label, description: i.description, detail: i.detail }),
        });
        if (answer.fallback) return nativePick(list, options, token) as Promise<T | undefined>;
        return answer.cancelled || answer.index === undefined ? undefined : list[answer.index];
    },
};
