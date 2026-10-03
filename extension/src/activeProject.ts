import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

/**
 * 배포·보안 화면이 대상으로 삼는 "현재 프로젝트".
 *
 * 왜 필요한가: 코드 생성이 워크스페이스 밖 폴더를 대상으로 하면 그 폴더를
 * 워크스페이스에 **추가**한다(멀티 루트). 그런데 배포 화면은 늘 첫 번째 폴더만
 * 봤기 때문에, `test temp` 에서 시작해 새 프로젝트를 만들어도 Deploy 에는
 * 계속 `test temp` 가 나왔다.
 *
 * 규칙
 * - 폴더가 하나면 그 폴더다(기존 동작과 같다).
 * - 새로 추가된 폴더가 현재 프로젝트가 된다. 사용자가 캔버스에서 바꿀 수 있다.
 * - 선택은 이 워크스페이스에만 저장한다. 선택한 폴더가 빠지면 첫 폴더로 돌아간다.
 * - 빈 창/단일 폴더 창에 폴더를 추가하면 VS Code 가 확장을 재시작한다(열려 있던 ReCoder
 *   화면이 끊기고 진행 중이던 요청이 사라졌다 — 실기기 영상 2026-10-03). 그래서 ReCoder 가
 *   고른 워크스페이스 밖 폴더는 **워크스페이스에 넣지 않고** "외부 프로젝트"로 기억해 쓴다.
 *   사용자가 대화상자에서 직접 고른 폴더만 들어온다. 재시작은 일어나지 않는다.
 * - 사용자가 직접 워크스페이스에 추가한 폴더는 예전처럼 새 현재 프로젝트가 된다.
 */
const SELECTED_KEY = 'recoder.activeProject';
const PENDING_KEY = 'recoder.activeProject.pending';
const PENDING_TTL_MS = 5 * 60 * 1000;
const EXTERNAL_KEY = 'recoder.externalProjects';
const MAX_EXTERNAL = 12;

let workspaceState: vscode.Memento | undefined;
let globalState: vscode.Memento | undefined;
let selected = '';
const emitter = new vscode.EventEmitter<string>();

/** 현재 프로젝트가 바뀌면 새 경로로 알린다. */
export const onDidChangeActiveProject: vscode.Event<string> = emitter.event;

function normalize(p: string): string {
    const resolved = path.resolve(p);
    return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
}

function folderPaths(): string[] {
    return (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath);
}

function findFolder(p: string): string | undefined {
    if (!p) { return undefined; }
    const key = normalize(p);
    return folderPaths().find((f) => normalize(f) === key);
}

function isDir(p: string): boolean {
    try { return fs.statSync(p).isDirectory(); } catch { return false; }
}

/** 사용자가 고른 워크스페이스 밖 프로젝트 폴더(아직 있는 것만). */
function externalPaths(): string[] {
    const list = workspaceState?.get<string[]>(EXTERNAL_KEY) ?? [];
    return (Array.isArray(list) ? list : []).filter((p) => typeof p === 'string' && !findFolder(p) && isDir(p));
}

function findExternal(p: string): string | undefined {
    if (!p) { return undefined; }
    const key = normalize(p);
    return externalPaths().find((f) => normalize(f) === key);
}

/** 프로젝트로 쓸 수 있는 폴더 — 워크스페이스 폴더 + 사용자가 고른 외부 폴더. 파일 쓰기도 이 안으로만 한다. */
export function projectRoots(): string[] {
    return [...folderPaths(), ...externalPaths()];
}

/** p 가 프로젝트 폴더이거나 그 안에 있는가. */
export function isInsideProjectRoot(p: string): boolean {
    return projectRoots().some((root) => {
        const rel = path.relative(root, p);
        return normalize(root) === normalize(p) || (!!rel && !rel.startsWith('..') && !path.isAbsolute(rel));
    });
}

/** 배포·검사 대상 폴더 경로. 워크스페이스가 없으면 ''. */
export function activeProjectPath(): string {
    return findFolder(selected) ?? findExternal(selected) ?? folderPaths()[0] ?? externalPaths()[0] ?? '';
}

export function activeProjectUri(): vscode.Uri | undefined {
    const p = activeProjectPath();
    return p ? vscode.Uri.file(p) : undefined;
}

export function projectFolders(): Array<{ path: string; name: string; active: boolean; external?: boolean }> {
    const active = activeProjectPath();
    return [
        ...(vscode.workspace.workspaceFolders ?? []).map((f) => ({
            path: f.uri.fsPath,
            name: f.name || path.basename(f.uri.fsPath),
            active: f.uri.fsPath === active,
        })),
        ...externalPaths().map((p) => ({ path: p, name: path.basename(p), active: p === active, external: true })),
    ];
}

/**
 * 워크스페이스 밖 폴더를 현재 프로젝트로 쓴다 — 워크스페이스는 건드리지 않는다(확장 재시작 없음).
 * 사용자가 폴더 선택 대화상자·승인 카드로 직접 고른 경로에만 호출한다.
 */
export async function useExternalProject(p: string): Promise<string> {
    const absolute = path.resolve(p);
    const inWorkspace = findFolder(absolute);
    if (inWorkspace) { await selectActiveProject(inWorkspace); return inWorkspace; }
    if (!isDir(absolute)) { throw new Error(`폴더가 없습니다: ${absolute}`); }
    const list = (workspaceState?.get<string[]>(EXTERNAL_KEY) ?? []).filter((x) => normalize(x) !== normalize(absolute));
    try { await workspaceState?.update(EXTERNAL_KEY, [absolute, ...list].slice(0, MAX_EXTERNAL)); } catch { /* 이번 세션만 */ }
    await selectActiveProject(absolute);
    return absolute;
}

/** 워크스페이스 폴더나 기억한 외부 폴더 중 하나를 현재 프로젝트로 고른다. 목록에 없으면 false. */
export async function selectActiveProject(p: string): Promise<boolean> {
    const hit = findFolder(p) ?? findExternal(p);
    if (!hit) { return false; }
    const before = activeProjectPath();
    selected = hit;
    try { await workspaceState?.update(SELECTED_KEY, hit); } catch { /* 저장 실패는 이번 세션 선택만 유지 */ }
    if (activeProjectPath() !== before) { emitter.fire(activeProjectPath()); }
    return true;
}

/** 폴더를 워크스페이스에 추가하기 **직전에** 호출한다(확장 재시작 대비). */
export async function markPendingActiveProject(p: string): Promise<void> {
    try { await globalState?.update(PENDING_KEY, { path: p, ts: Date.now() }); } catch { /* ignore */ }
}

function consumePending(): string {
    const pending = globalState?.get<{ path?: string; ts?: number }>(PENDING_KEY);
    if (!pending?.path || typeof pending.ts !== 'number') { return ''; }
    const fresh = Date.now() - pending.ts < PENDING_TTL_MS;
    const hit = fresh ? findFolder(pending.path) : undefined;
    // 이 창에 그 폴더가 들어왔거나 만료됐을 때만 지운다. 다른 창이 가져가면 안 된다.
    if (hit || !fresh) { void globalState?.update(PENDING_KEY, undefined); }
    return hit ?? '';
}

/** 확장 활성화 시 한 번 호출. 폴더 변경을 따라가며 선택을 갱신한다. */
export function initActiveProject(context: Pick<vscode.ExtensionContext, 'workspaceState' | 'globalState' | 'subscriptions'>): void {
    workspaceState = context.workspaceState;
    globalState = context.globalState;
    const saved = workspaceState.get<string>(SELECTED_KEY) ?? '';
    selected = consumePending() || findFolder(saved) || findExternal(saved) || '';
    if (selected) { void workspaceState.update(SELECTED_KEY, selected); }
    context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders((e) => {
        const before = activeProjectPath();
        const added = e.added[e.added.length - 1];
        if (added) {
            selected = added.uri.fsPath;
            void workspaceState?.update(SELECTED_KEY, selected);
            void globalState?.update(PENDING_KEY, undefined);
        } else if (!findFolder(selected) && !findExternal(selected)) {
            selected = '';
            void workspaceState?.update(SELECTED_KEY, undefined);
        }
        if (activeProjectPath() !== before) { emitter.fire(activeProjectPath()); }
    }));
}

/**
 * "폴더 변경" — 워크스페이스의 다른 폴더나 예전에 고른 외부 폴더를 고르거나, 새 폴더를 고른다.
 *
 * 새 폴더는 워크스페이스에 넣지 않고 외부 프로젝트로 기억한다 — 넣으면 VS Code 가 확장을
 * 재시작해 화면이 끊겼다. 열린 파일·터미널도 그대로다. 고른 폴더가 곧바로 현재 프로젝트가 된다.
 * 반환: 'selected' | 'added' | 'unchanged'
 */
export async function pickProjectFolder(): Promise<'selected' | 'added' | 'unchanged'> {
    const active = activeProjectPath();
    type Item = vscode.QuickPickItem & { value: string };
    const items: Item[] = (vscode.workspace.workspaceFolders ?? []).map((f) => ({
        label: `$(folder) ${f.name}`,
        description: f.uri.fsPath === active ? '현재 프로젝트' : '',
        detail: f.uri.fsPath,
        value: f.uri.fsPath,
    }));
    for (const p of externalPaths()) {
        items.push({ label: `$(folder) ${path.basename(p)}`, description: p === active ? '현재 프로젝트' : '워크스페이스 밖', detail: p, value: p });
    }
    items.push({ label: '$(folder-opened) 다른 폴더 선택…', description: '이 폴더로 바꿉니다(창은 그대로)', value: '' });
    const pick = await vscode.window.showQuickPick(items, { title: 'ReCoder: 배포할 프로젝트 폴더', placeHolder: '배포·보안 검사 대상 폴더를 고르세요' });
    if (!pick) { return 'unchanged'; }
    if (pick.value) {
        if (normalize(pick.value) === normalize(active)) { return 'unchanged'; }
        return (await selectActiveProject(pick.value)) ? 'selected' : 'unchanged';
    }
    const chosen = await vscode.window.showOpenDialog({
        canSelectFolders: true, canSelectFiles: false, canSelectMany: false,
        openLabel: '이 폴더로 변경', title: 'ReCoder: 배포할 프로젝트 폴더 선택',
        defaultUri: active ? vscode.Uri.file(path.dirname(active)) : undefined,
    });
    const folder = chosen?.[0]?.fsPath;
    if (!folder) { return 'unchanged'; }
    if (findFolder(folder)) {
        return (await selectActiveProject(folder)) ? 'selected' : 'unchanged';
    }
    //: 워크스페이스에 추가하지 않는다 — 단일 폴더 창에 추가하면 VS Code 가 확장을 재시작해
    //: 열려 있던 ReCoder 화면이 끊겼다. 외부 프로젝트로 기억하고 바로 바꾼다.
    await useExternalProject(folder);
    return 'selected';
}
