import { execFile } from 'child_process';
import { promisify } from 'util';
import * as fs from 'fs';
import * as path from 'path';
import { createHash } from 'crypto';
import { isSensitiveFile } from '../deploy/staticSite';

const exec = promisify(execFile);
export interface CommitFile { path: string; status: string; blocked: string }
export interface CommitPreview { files: CommitFile[]; fingerprint: string; name: string; email: string }
async function git(root: string, args: string[]) {
  return (await exec('git', ['-C', root, '--literal-pathspecs', ...args], {
    windowsHide: true, timeout: 30000, maxBuffer: 4 * 1024 * 1024,
    env: { ...process.env, GIT_TERMINAL_PROMPT: '0' },
  })).stdout;
}
function blockedFile(root: string, file: string): string {
  if (file.split('/').some(p => ['node_modules', '.recoder', '.aws', '.ssh', '.venv', 'venv', '.git'].includes(p)) || isSensitiveFile(file)) return '자격증명·로컬 설정 또는 의존성 파일';
  const full = path.resolve(root, file), relative = path.relative(root, full);
  if (relative.startsWith('..') || path.isAbsolute(relative)) return '프로젝트 밖의 경로';
  try {
    if (fs.lstatSync(full).isSymbolicLink()) return '심볼릭 링크';
    if (fs.statSync(full).size > 10 * 1024 * 1024) return '10 MB 초과: 소스 제어에서 확인';
  } catch (error: any) { if (error.code !== 'ENOENT') throw error; }
  return '';
}
export async function previewCommit(root: string): Promise<CommitPreview> {
  const raw = await git(root, ['status', '--porcelain=v1', '-z', '--untracked-files=all']);
  const files: CommitFile[] = [], parts = raw.split('\0').filter(Boolean);
  for (let i = 0; i < parts.length; i++) {
    const status = parts[i].slice(0, 2), file = parts[i].slice(3);
    if (/U|AA|DD/.test(status)) throw new Error('충돌을 해결한 뒤 다시 커밋하세요.');
    files.push({ path: file, status, blocked: blockedFile(root, file) });
    if (/[RC]/.test(status) && parts[i + 1]) {
      const previous = parts[++i];
      if (status.includes('R')) files.push({ path: previous, status: 'D ', blocked: blockedFile(root, previous) });
    }
  }
  if (files.length > 1000) throw new Error('변경 파일이 1,000개를 넘습니다. .gitignore에 빌드 결과와 의존성 폴더를 제외하세요.');
  const hash = createHash('sha256').update(raw);
  hash.update(await git(root, ['rev-parse', '--verify', 'HEAD']).catch(() => ''));
  hash.update(await git(root, ['diff', '--cached', '--binary']));
  for (const file of files.filter(f => !f.blocked)) {
    hash.update(file.path);
    try { hash.update(fs.readFileSync(path.join(root, file.path))); }
    catch (error: any) { if (error.code !== 'ENOENT') throw error; hash.update('[deleted]'); }
  }
  const [name, email] = await Promise.all(['user.name', 'user.email'].map(key => git(root, ['config', key]).catch(() => '')));
  return { files, fingerprint: hash.digest('hex'), name: name.trim(), email: email.trim() };
}
export async function commitSelected(root: string, preview: CommitPreview, selected: string[], message: string, name: string, email: string): Promise<boolean> {
  const current = await previewCommit(root);
  if (current.fingerprint !== preview.fingerprint) throw new Error('확인 이후 파일이 변경되었습니다. 변경 파일을 다시 확인하세요.');
  if (!message.trim() || message.length > 500) throw new Error('500자 이내의 커밋 메시지를 입력하세요.');
  if (!name.trim() || /[<>\r\n]/.test(name) || !/^[^<>\s@]+@[^<>\s@]+$/.test(email)) throw new Error('커밋 작성자 이름과 이메일을 확인하세요.');
  const allowed = new Set(current.files.filter(file => !file.blocked).map(file => file.path));
  if (!selected.length || selected.some(file => !allowed.has(file)) || new Set(selected).size !== selected.length) throw new Error('확인한 파일만 선택해서 커밋할 수 있습니다.');
  await git(root, ['add', '-A', '--', ...selected]);
  // An added-then-deleted file disappears from the index after add -A. On an
  // unborn branch it never existed in HEAD either, so commit --only rejects it.
  const known = new Set((await git(root, ['ls-files', '--cached', '-z', '--', ...selected])
    + await git(root, ['ls-tree', '-r', '--name-only', '-z', 'HEAD', '--', ...selected]).catch(() => '')).split('\0'));
  const paths = selected.filter(file => known.has(file));
  if (!paths.length) return false;
  // --only preserves unrelated files already staged by the user, including on
  // the first commit. No editor or automatic push is involved.
  try {
    await git(root, ['-c', `user.name=${name.trim()}`, '-c', `user.email=${email.trim()}`, 'commit', '--only', '-m', message.trim(), '--', ...paths]);
  } catch (error: any) {
    throw new Error(`커밋하지 못했습니다: ${String(error.stderr || error.stdout || 'Git 상태를 새로고침하세요.').trim().slice(-1600)}`);
  }
  return true;
}
