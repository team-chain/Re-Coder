/**
 * 보안 검사 → 수정안 → 적용 → 다시 검사. 화면 로직(순수 함수)만 모았다(테스트 가능).
 *
 * 예전 보안 검사는 "심각 3 · 높음 12" 만 보여 주고 끝났다. 코어가 결과로 결정론적 수정안을
 * 만들고(security_fix.py), 화면은 고른 것만 적용한 뒤 바뀐 부분을 다시 검사한다.
 */
import type { ScanKind } from './scanQueue';

export interface FixProposal {
  id: string; tool: string; title: string; detail: string; files: string[]; diff: string;
  auto: boolean; risk: string; note: string; rebuild: boolean;
}

export interface FixApplyResult {
  applied: string[]; skipped: { id: string; reason: string }[]; changed: string[];
  backups?: string[]; notes: string[]; rebuild: boolean;
}

interface ResultLike { requestId?: string; status?: string; findings?: unknown; workspace?: string }

/** 수정안을 만들 검사 결과(정상 완료 + 발견 있음)만. 없으면 null. */
export function fixReports(results: Partial<Record<ScanKind, ResultLike>>): { key: string; workspace?: string; reports: Record<string, { findings: unknown[] }> } | null {
  const reports: Record<string, { findings: unknown[] }> = {};
  const keys: string[] = [];
  let workspace: string | undefined;
  for (const kind of ['trivy', 'hadolint', 'gitleaks'] as ScanKind[]) {
    const r = results[kind];
    if (!r || r.status !== 'ok' || !Array.isArray(r.findings) || r.findings.length === 0) continue;
    reports[kind] = { findings: r.findings };
    keys.push(`${kind}:${r.requestId ?? ''}`);
    workspace = workspace ?? r.workspace;
  }
  return keys.length ? { key: keys.join('|'), workspace, reports } : null;
}

/** 기본 선택: 자동으로 고칠 수 있고 위험 표시가 없는 것. */
export function defaultSelection(proposals: FixProposal[]): Set<string> {
  return new Set(proposals.filter(p => p.auto && !p.risk).map(p => p.id));
}

/** 적용한 수정에 따라 다시 볼 검사와 이미지 재빌드 여부. */
export function followUp(applied: FixProposal[], results: Partial<Record<ScanKind, ResultLike>>): { rescan: ScanKind[]; rebuild: boolean } {
  const rescan = new Set<ScanKind>();
  const touchesDockerfile = applied.some(p => p.files.some(f => f === 'Dockerfile' || f.endsWith('/Dockerfile')));
  if (touchesDockerfile && results.hadolint) rescan.add('hadolint');
  if (applied.some(p => p.tool === 'gitleaks')) rescan.add('gitleaks');
  //: 이미지 취약점은 다시 빌드해야 바뀐다 — 지난번에 이미지 검사가 실제로 돌았을 때만(빌드된 이미지가 있을 때).
  const rebuild = applied.some(p => p.rebuild) && results.trivy?.status === 'ok';
  return { rescan: [...rescan], rebuild };
}

/** diff 한 줄의 색(추가·삭제·머리). */
export function diffLineKind(line: string): 'add' | 'del' | 'meta' | 'ctx' {
  if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) return 'meta';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return 'ctx';
}
