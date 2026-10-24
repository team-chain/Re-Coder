/**
 * 3.0.2 홈의 "바뀐 코드" 칸 — 아직 커밋하지 않은 변경을 읽기만 한다(아무것도 바꾸지 않는다).
 *
 *  · git diff --numstat 으로 파일별 +/− 를, 첫 파일의 첫 변경 부분만 몇 줄 미리 보여 준다.
 *  · .env·자격증명·의존성 폴더처럼 커밋 화면이 막는 파일은 목록에서도 빼고 내용은 절대 읽지 않는다.
 */
import { execFile } from 'child_process';
import { promisify } from 'util';
import { blockedFile } from './githubCommit';

const exec = promisify(execFile);

export interface ChangedFile { path: string; added: number; removed: number; binary: boolean }
export interface WorkingChanges {
    files: ChangedFile[]; added: number; removed: number; untracked: number;
    snippet: null | { file: string; lines: Array<{ kind: 'add' | 'del' | 'ctx'; text: string }> };
    error?: string;
}

async function git(root: string, args: string[]): Promise<string> {
    return (await exec('git', ['-C', root, '--literal-pathspecs', ...args], {
        windowsHide: true, timeout: 5000, maxBuffer: 2 * 1024 * 1024,
        env: { ...process.env, GIT_TERMINAL_PROMPT: '0' },
    })).stdout;
}

export function parseNumstat(raw: string): ChangedFile[] {
    const out: ChangedFile[] = [];
    for (const line of raw.split('\n')) {
        const m = /^(\d+|-)\t(\d+|-)\t(.+)$/.exec(line.trim());
        if (!m) { continue; }
        const binary = m[1] === '-' || m[2] === '-';
        //: 이름 바꾸기는 "a => b" 또는 "dir/{a => b}" 로 온다 — 새 이름만 남긴다.
        const file = m[3].replace(/\{[^{}]* => ([^{}]*)\}/, '$1').replace(/^.* => /, '');
        out.push({ path: file, added: binary ? 0 : Number(m[1]), removed: binary ? 0 : Number(m[2]), binary });
    }
    return out;
}

export function parseFirstHunk(raw: string, max = 6): Array<{ kind: 'add' | 'del' | 'ctx'; text: string }> {
    const lines: Array<{ kind: 'add' | 'del' | 'ctx'; text: string }> = [];
    let inHunk = false;
    for (const line of raw.split('\n')) {
        if (line.startsWith('@@')) { if (inHunk) { break; } inHunk = true; continue; }
        if (!inHunk || line.startsWith('\\')) { continue; }
        const kind = line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : 'ctx';
        lines.push({ kind, text: line.slice(1).replace(/\t/g, '  ').slice(0, 140) });
        if (lines.length >= max) { break; }
    }
    return lines;
}

export async function workingChanges(root: string): Promise<WorkingChanges> {
    const empty: WorkingChanges = { files: [], added: 0, removed: 0, untracked: 0, snippet: null };
    if (!root) { return empty; }
    try {
        const head = await git(root, ['rev-parse', '--verify', 'HEAD']).then(() => true, () => false);
        //: --relative: 프로젝트가 더 큰 저장소의 하위 폴더여도 이 폴더 안의 변경만(경로도 이 폴더 기준).
        const numstat = await git(root, head ? ['diff', '--numstat', '--relative', 'HEAD', '--'] : ['diff', '--numstat', '--relative', '--cached', '--']);
        const files = parseNumstat(numstat).filter(f => !blockedFile(root, f.path)).slice(0, 200);
        const untracked = (await git(root, ['ls-files', '--others', '--exclude-standard']).catch(() => ''))
            .split('\n').filter(f => f.trim() && !blockedFile(root, f.trim())).length;
        const first = files.find(f => !f.binary && (f.added || f.removed));
        let snippet: WorkingChanges['snippet'] = null;
        if (first && head) {
            const diff = await git(root, ['diff', '-U1', '--relative', 'HEAD', '--', first.path]).catch(() => '');
            const lines = parseFirstHunk(diff);
            if (lines.length) { snippet = { file: first.path, lines }; }
        }
        return {
            files, untracked, snippet,
            added: files.reduce((n, f) => n + f.added, 0), removed: files.reduce((n, f) => n + f.removed, 0),
        };
    } catch {
        return { ...empty, error: 'Git 변경을 읽지 못했습니다.' };
    }
}
