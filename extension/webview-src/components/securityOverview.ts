import { gateVerdict, findingCount, GateScanResult } from './securityGate';
import { ScanKind } from './scanQueue';
import type {ManagedReport} from '../../src/security/managedReportTypes';
export interface CodeFinding { rule: string; label: string; file: string; line: number; message: string; severity: string; fix: string; precise?: boolean }
export interface CodeReport { score: number | null; findings: CodeFinding[]; files_checked?: number; scope?: string }
export const SECURITY_KINDS: ScanKind[] = ['trivy', 'hadolint', 'gitleaks'];
export function overview(results: Partial<Record<ScanKind, GateScanResult>>, code?: CodeReport, running = false, codeError = '', managed?:ManagedReport) {
  const gate = gateVerdict(SECURITY_KINDS, results, running);
  const findings = code?.findings ?? [];
  const count = findings.length + (managed? (managed.scans?.checks??[]).reduce((n,c)=>n+(c.count??c.findings.length),0): SECURITY_KINDS.reduce((n,k) => n + (results[k]?.status === 'ok' ? findingCount(results[k]!) : 0), 0));
  const incomplete = managed ? managed.status!=='completed'||managed.mode!=='scans'||managed.scans?.checks.length!==4||managed.scans.checks.some(c=>c.status!=='completed')||!managed.scans.policy : SECURITY_KINDS.some(k => results[k]?.status !== 'ok');
  const partial = !!incomplete || !code || !!codeError || code.score === null;
  const denied = managed?.scans?.policy?.decision==='deny';
  const title = running ? '보안 문제를 확인하고 있습니다' : count ? `확인할 보안 항목 ${count}개가 있습니다` : partial ? '아직 확인하지 못한 검사가 있습니다' : '검사한 범위에서 문제를 찾지 못했습니다';
  return {count, title:denied&&!running&&!count?'검사 사본의 배포 정책을 충족하지 못했습니다':title, tone: count||denied ? 'warn' as const : running || partial ? 'off' as const : 'ok' as const, gate, partial};
}
