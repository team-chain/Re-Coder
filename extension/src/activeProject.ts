import * as vscode from 'vscode';
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
 * - 빈 창/단일 폴더 창에 폴더를 추가하면 VS Code 가 확장을 재시작한다. 그래서
 *   추가 직전에 globalState 에 "대기 중 선택"을 적어 두고 재시작 후 이어받는다.
 */
const SELECTED_KEY = 'recoder.activeProject';
const PENDING_KEY = 'recoder.activeProject.pending';
const PENDING_TTL_MS = 5 * 60 * 1000;

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

/** 배포·검사 대상 폴더 경로. 워크스페이스가 없으면 ''. */
export function activeProjectPath(): string {
    return findFolder(selected) ?? folderPaths()[0] ?? '';
}

export function activeProjectUri(): vscode.Uri | undefined {
    const p = activeProjectPath();
    return p ? vscode.Uri.file(p) : undefined;
}

export function projectFolders(): Array<{ path: string; name: string; active: boolean }> {
    const active = activeProjectPath();
    return (vscode.workspace.workspaceFolders ?? []).map((f) => ({
        path: f.uri.fsPath,
        name: f.name || path.basename(f.uri.fsPath),
        active: f.uri.fsPath === active,
    }));
}

/** 워크스페이스 폴더 중 하나를 현재 프로젝트로 고른다. 목록에 없으면 false. */
export async function selectActiveProject(p: string): Promise<boolean> {
    const hit = findFolder(p);
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
    selected = consumePending() || findFolder(workspaceState.get<string>(SELECTED_KEY) ?? '') || '';
    if (selected) { void workspaceState.update(SELECTED_KEY, selected); }
    context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders((e) => {
        const before = activeProjectPath();
        const added = e.added[e.added.length - 1];
        if (added) {
            selected = added.uri.fsPath;
            void workspaceState?.update(SELECTED_KEY, selected);
            void globalState?.update(PENDING_KEY, undefined);
        } else if (!findFolder(selected)) {
            selected = '';
            void workspaceState?.update(SELECTED_KEY, undefined);
        }
        if (activeProjectPath() !== before) { emitter.fire(activeProjectPath()); }
    }));
}
