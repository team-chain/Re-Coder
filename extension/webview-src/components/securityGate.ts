/**
 * 보안 검사 결과 → 게이트 판정. React 없이 테스트할 수 있게 분리한다.
 *
 * 판정 규칙
 * - 발견 항목이 하나라도 있으면 `issues`(빨강). 시크릿은 심각도와 상관없이 이상이다.
 * - 검사한 항목이 모두 깨끗하면 `clean`(초록). 검사 대상 자체가 아직 없는 경우
 *   (Dockerfile 없음, 빌드된 이미지 없음)는 "해당 없음"으로 보고 초록을 막지 않는다.
 *   단 소스 시크릿 검사만큼은 실제로 통과해야 초록이다.
 * - 도구 미설치·Docker 꺼짐·오류처럼 검사를 못 한 항목이 있으면 `unverified`(노랑).
 *   못 한 검사를 통과로 표시하지 않는다.
 */
export type GateKind = 'trivy' | 'hadolint' | 'gitleaks';
export interface GateScanResult {
  scan_type?: string;
  status?: string;
  reason_code?: string;
  critical_count?: number;
  high_count?: number;
  medium_count?: number;
  findings?: unknown;
}
export type GateState = 'idle' | 'running' | 'clean' | 'issues' | 'unverified';
export interface GateVerdict { state: GateState; label: string; issues: number; notApplicable: GateKind[]; unverified: GateKind[] }

/** 검사 대상이 아직 없을 뿐인 결과. 위험 판정도 통과 판정도 아니다. */
const NOT_APPLICABLE = new Set(['image_not_found', 'dockerfile_missing']);

export function findingCount(r: GateScanResult): number {
  const listed = Array.isArray(r.findings) ? r.findings.length : 0;
  const counted = (r.critical_count ?? 0) + (r.high_count ?? 0) + (r.medium_count ?? 0);
  return Math.max(listed, counted);
}

export function isNotApplicable(r: GateScanResult | undefined, kind: GateKind): boolean {
  return Boolean(r && r.status !== 'ok' && kind !== 'gitleaks' && NOT_APPLICABLE.has(String(r.reason_code || '')));
}

export function gateVerdict(kinds: GateKind[], results: Partial<Record<GateKind, GateScanResult>>, running: boolean): GateVerdict {
  const notApplicable: GateKind[] = [], unverified: GateKind[] = [];
  let issues = 0, clean = 0, ran = 0;
  for (const kind of kinds) {
    const r = results[kind];
    if (!r) continue;
    ran++;
    if (r.status === 'ok') {
      const n = findingCount(r);
      if (n) issues += n; else clean++;
    } else if (isNotApplicable(r, kind)) notApplicable.push(kind);
    else unverified.push(kind);
  }
  if (running) return { state: 'running', label: '검사 중', issues, notApplicable, unverified };
  if (!ran) return { state: 'idle', label: '검사 대기', issues, notApplicable, unverified };
  if (issues) return { state: 'issues', label: `이상 발견 ${issues}건`, issues, notApplicable, unverified };
  const secretsPassed = !kinds.includes('gitleaks') || results.gitleaks?.status === 'ok';
  if (ran === kinds.length && !unverified.length && clean > 0 && secretsPassed) {
    return { state: 'clean', label: '이상 없음', issues, notApplicable, unverified };
  }
  const pending = kinds.length - ran + unverified.length;
  return { state: 'unverified', label: `검사 미확인 ${pending}개`, issues, notApplicable, unverified };
}

export const gateColors: Record<GateState, string> = {
  idle: '#75808f', running: '#f4cd65', clean: '#36c77a', issues: '#ff6a73', unverified: '#f4cd65',
};
