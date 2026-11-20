/**
 * 보안 검사 결과 → 처음 보는 사람도 읽을 수 있는 위험 목록과 판정.
 *
 * 원칙
 * - 검사하지 못한 항목은 통과로 세지 않는다(판정은 "주의" 이상, 단계 점은 노랑).
 * - 쉬운 말 제목은 취약점 제목·CWE 의 키워드로만 고른다. 모르면 "알려진 취약점" — 지어내지 않는다.
 * - 같은 패키지의 여러 CVE 는 한 줄로 묶는다(가장 높은 심각도, 고칠 버전이 하나라도 있으면 고칠 수 있음).
 */
import type { ScanKind } from './scanQueue';
import type { CodeReport } from './securityOverview';
import { gateVerdict, isNotApplicable, type GateScanResult } from './securityGate';
import type { PentagiAssessment, CheckKind } from '../../src/security/pentagiAssessmentTypes';

export type RiskLevel = 'danger' | 'caution' | 'low';
export type RiskSource = 'image' | 'dockerfile' | 'secret' | 'code' | 'attack';
export type RiskFix = 'auto' | 'code' | 'wait' | 'manual';
export interface RiskDetail { id?: string; title?: string; installed?: string; fixed?: string; file?: string; line?: number; where?: string; os?: boolean; path?: string }
export interface Risk { key: string; level: RiskLevel; title: string; where: string; source: RiskSource; fix: RiskFix; count: number; details: RiskDetail[] }
export type Verdict = 'idle' | 'running' | 'safe' | 'caution' | 'danger';
export type StepState = 'idle' | 'running' | 'ok' | 'warn' | 'bad' | 'na';
export interface Step { key: 'gitleaks' | 'hadolint' | 'trivy' | 'code' | 'policy' | 'watch' | 'attack'; label: string; state: StepState }
export interface AttackRow { kind: CheckKind; label: string; state: 'blocked' | 'found' | 'review' | 'unverified' | 'untested'; requests: number }

const LEVEL_RANK: Record<RiskLevel, number> = { danger: 3, caution: 2, low: 1 };

/** 취약점 제목·CWE → 무슨 일이 생길 수 있는지(짧은 말). 순서가 우선순위다. */
const IMPACT: Array<[RegExp, string, string[]]> = [
  [/remote code execution|\brce\b|command injection|code execution|arbitrary code|code injection|deserializ/i, '서버 장악', ['CWE-77', 'CWE-78', 'CWE-94', 'CWE-502']],
  [/sql injection|\bsqli\b/i, 'DB 공격', ['CWE-89']],
  [/authentication bypass|auth(?:entication)? bypass|improper authentication|missing authentication/i, '로그인 우회', ['CWE-287', 'CWE-306']],
  [/privilege escalation|authori[sz]ation|access control|permission/i, '권한 우회', ['CWE-269', 'CWE-285', 'CWE-862', 'CWE-863', 'CWE-639']],
  [/server-side request forgery|\bssrf\b/i, '내부망 접근', ['CWE-918']],
  [/path traversal|directory traversal|arbitrary file (?:read|write)|zip slip/i, '파일 노출', ['CWE-22', 'CWE-23', 'CWE-35']],
  [/cross-site scripting|\bxss\b|html injection/i, '화면 조작', ['CWE-79', 'CWE-80']],
  [/request forgery|\bcsrf\b/i, '요청 위조', ['CWE-352']],
  [/open redirect/i, '피싱 이동', ['CWE-601']],
  [/prototype pollution/i, '데이터 변조', ['CWE-1321']],
  [/certificate|\btls\b|\bssl\b|crypt|signature|timing side|downgrade/i, '통신 유출', ['CWE-295', 'CWE-326', 'CWE-327', 'CWE-347']],
  [/information (?:disclosure|exposure)|sensitive|\bleak|exposure of/i, '정보 유출', ['CWE-200', 'CWE-209', 'CWE-532']],
  [/buffer overflow|out-of-bounds|use[- ]after[- ]free|heap|memory corruption|integer overflow|double free|null pointer/i, '서버 손상', ['CWE-119', 'CWE-120', 'CWE-125', 'CWE-787', 'CWE-416', 'CWE-190', 'CWE-476']],
  [/denial of service|\bdos\b|resource exhaustion|infinite loop|redos|regular expression|uncontrolled recursion|memory exhaustion|crash|panic|hang|excessive|allocation/i, '서버 멈춤', ['CWE-400', 'CWE-770', 'CWE-835', 'CWE-1333', 'CWE-674', 'CWE-789']],
];

export function impactOf(title: string, cwe: string[] = []): string {
  for (const [re, label, cwes] of IMPACT) if (re.test(title) || cwe.some(c => cwes.includes(String(c).toUpperCase()))) return label;
  return '알려진 취약점';
}

/** 코드 규칙 → 쉬운 말. code_security.RULES 와 맞춘다. */
const CODE_IMPACT: Record<string, string> = {
  SQL_INJECTION: 'DB 공격', MISSING_AUTH: '로그인 없이 변경', OWNER_CHECK: '남의 데이터 변경', JWT_UNSAFE: '로그인 위조',
  PLAINTEXT_PASSWORD: '비밀번호 노출', CORS_WILDCARD: '다른 사이트 접근', XSS_HTML: '화면 조작', BRUTE_FORCE: '비밀번호 대입', ERROR_LEAK: '내부 정보 노출',
};

const sev = (v: unknown) => String(v ?? '').toUpperCase();
const levelOf = (s: string): RiskLevel => s === 'CRITICAL' ? 'danger' : s === 'HIGH' ? 'caution' : 'low';
const str = (v: unknown) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const list = (r?: GateScanResult) => (r?.status === 'ok' && Array.isArray(r.findings) ? (r.findings as Record<string, unknown>[]) : []);

function imageRisks(r?: GateScanResult): Risk[] {
  const byPkg = new Map<string, Risk>();
  for (const f of list(r)) {
    const pkg = str(f.package || f.PkgName) || '이미지 패키지';
    const s = sev(f.severity || f.Severity);
    const level = levelOf(s);
    const title = str(f.title || f.Title);
    const cwe = Array.isArray(f.cwe) ? (f.cwe as unknown[]).map(str) : [];
    const detail: RiskDetail = { id: str(f.id || f.VulnerabilityID) || undefined, title: title || undefined, installed: str(f.installed || f.InstalledVersion) || undefined, fixed: str(f.fixed || f.FixedVersion) || undefined, where: str(f.target) || undefined, path: str(f.pkg_path || f.PkgPath) || undefined, os: str(f.class) === 'os-pkgs' || /\((?:alpine|debian|ubuntu|wolfi|rhel|centos|amazon|redhat|rocky|alma)/i.test(str(f.target)) || undefined };
    const cur = byPkg.get(pkg);
    const impact = impactOf(title, cwe);
    if (!cur) {
      byPkg.set(pkg, { key: `image:${pkg}`, level, title: impact, where: pkg, source: 'image', fix: detail.fixed ? 'auto' : 'wait', count: 1, details: [detail] });
    } else {
      cur.count++; cur.details.push(detail);
      if (LEVEL_RANK[level] > LEVEL_RANK[cur.level]) { cur.level = level; cur.title = impact; }
      else if (cur.title === '알려진 취약점' && impact !== cur.title) cur.title = impact;
      if (detail.fixed) cur.fix = 'auto';
    }
  }
  //: 어느 이미지(서비스)에 들어 있는지 — Trivy target "shop-web (alpine 3.22)" → "shop-web".
  for (const r of byPkg.values()) {
    //: 이미지 OS 패키지는 이미지 이름, 언어 패키지는 앱 의존성인지 이미지에 딸린 npm 인지로 말한다("Node.js" 같은 Trivy 분류명은 쓰지 않는다).
    const imgs = [...new Set(r.details.map(d => d.os || /\(/.test(d.where ?? '') ? (d.where ?? '').split(' (')[0].trim() : /node_modules\/npm\//.test(d.path ?? '') ? '이미지의 npm' : d.path ? '앱 의존성' : '').filter(Boolean))];
    if (imgs.length) r.where = `${r.where} · ${imgs.slice(0, 3).join(', ')}${imgs.length > 3 ? ` 외 ${imgs.length - 3}` : ''}`;
  }
  return [...byPkg.values()];
}

//: hadolint 를 표준 입력으로 돌리면 파일 이름이 "-" 로 온다.
const dfName = (v: unknown) => { const n = str(v); return !n || n === '-' ? 'Dockerfile' : n; };
function dockerfileRisks(r?: GateScanResult): Risk[] {
  return list(r).map((f, i) => {
    const s = sev(f.severity);
    return { key: `dockerfile:${str(f.code)}:${str(f.line)}:${i}`, level: levelOf(s), title: '컨테이너 설정', where: `${dfName(f.file)}${f.line ? `:${f.line}` : ''}`, source: 'dockerfile' as const, fix: 'auto' as const, count: 1,
      details: [{ id: str(f.code) || undefined, title: str(f.message) || undefined, file: dfName(f.file), line: Number(f.line) || undefined }] };
  });
}

function secretRisks(r?: GateScanResult): Risk[] {
  return list(r).map((f, i) => ({ key: `secret:${str(f.file)}:${str(f.line)}:${i}`, level: 'danger' as const, title: '키 노출', where: `${str(f.file) || '파일'}${f.line ? `:${f.line}` : ''}`, source: 'secret' as const, fix: 'manual' as const, count: 1,
    details: [{ id: str(f.rule_id) || undefined, title: str(f.secret_type || f.description) || '코드에 비밀값이 들어 있습니다. 값을 바꾸고(재발급) 코드에서 빼야 합니다.', file: str(f.file) || undefined, line: Number(f.line) || undefined }] }));
}

function codeRisks(code?: CodeReport): Risk[] {
  return (code?.findings ?? []).map((f, i) => {
    const s = sev(f.severity);
    return { key: `code:${f.rule}:${f.file}:${f.line}:${i}`, level: s === 'CRITICAL' ? 'danger' : s === 'HIGH' ? 'caution' : 'low', title: CODE_IMPACT[f.rule] ?? (f.label || '코드 보안 문제'), where: `${f.file}:${f.line}`, source: 'code' as const, fix: 'code' as const, count: 1,
      details: [{ id: f.label, title: f.message, file: f.file, line: f.line }] };
  });
}

const ATTACK_LABEL: Record<CheckKind, string> = { discovery: '숨은 경로 찾기', sql_injection: 'DB 공격', authentication: '로그인 우회', authorization: '남의 데이터 보기', input_handling: '입력값 조작' };
function attackRisks(a?: PentagiAssessment): Risk[] {
  return (a?.findings ?? []).map((f, i) => ({ key: `attack:${f.kind}:${f.route}:${i}`, level: f.evidence === 'http_observed' ? 'danger' as const : 'caution' as const, title: ATTACK_LABEL[f.kind] ?? '공격 성공', where: f.route, source: 'attack' as const, fix: 'code' as const, count: 1,
    details: [{ title: f.observation || f.title, where: f.evidence === 'http_observed' ? '실제 응답으로 확인' : 'AI 후보 · 확인 필요' }] }));
}

export function attackRows(a?: PentagiAssessment): AttackRow[] {
  const kinds = Object.keys(ATTACK_LABEL) as CheckKind[];
  return kinds.map(kind => {
    const check = a?.checks.find(c => c.kind === kind);
    const found = a?.findings.filter(f => f.kind === kind) ?? [];
    //: tested + 발견 없음 = 막음. blocked = 검사 도구가 확인할 수 없었음(계정·규칙 없음 등) — 막았다고 하지 않는다.
    const state: AttackRow['state'] = found.some(f => f.evidence === 'http_observed') ? 'found' : found.length ? 'review' : check?.status === 'tested' ? 'blocked' : check?.status === 'blocked' ? 'unverified' : 'untested';
    return { kind, label: ATTACK_LABEL[kind], state, requests: check?.requests.length ?? 0 };
  });
}

export interface SecurityInput {
  scans: Partial<Record<ScanKind, GateScanResult>>;
  scanning: boolean;
  code?: CodeReport; codeRunning?: boolean; codeError?: string;
  policy?: { running?: boolean; error?: string; result?: Record<string, unknown> };
  watch?: { running?: boolean; error?: string; result?: Record<string, unknown> };
  attack?: PentagiAssessment; attackRunning?: boolean;
}

export function securityModel(input: SecurityInput) {
  const { scans, scanning, code } = input;
  const risks = [...attackRisks(input.attack), ...secretRisks(scans.gitleaks), ...codeRisks(code), ...imageRisks(scans.trivy), ...dockerfileRisks(scans.hadolint)]
    .sort((a, b) => LEVEL_RANK[b.level] - LEVEL_RANK[a.level] || (a.fix === 'auto' ? -1 : 0) - (b.fix === 'auto' ? -1 : 0));
  const tally = { danger: 0, caution: 0, low: 0 } as Record<RiskLevel, number>;
  risks.forEach(r => tally[r.level]++);

  const stepOf = (k: ScanKind): StepState => {
    const r = scans[k];
    if (scanning && !r) return 'running';
    if (!r) return 'idle';
    if (isNotApplicable(r, k)) return 'na';
    if (r.status !== 'ok') return 'warn';
    const own = k === 'trivy' ? imageRisks(r) : k === 'hadolint' ? dockerfileRisks(r) : secretRisks(r);
    return own.some(x => x.level === 'danger') ? 'bad' : own.some(x => x.level === 'caution') ? 'warn' : 'ok';
  };
  const codeState: StepState = input.codeRunning ? 'running' : input.codeError ? 'warn' : !code ? 'idle' : code.score === null ? 'na' : codeRisks(code).some(r => r.level !== 'low') ? 'warn' : 'ok';
  const pol = input.policy;
  const policyState: StepState = pol?.running ? 'running' : pol?.error ? 'warn' : !pol?.result ? 'idle' : pol.result.opa_available ? 'ok' : 'warn';
  const w = input.watch;
  //: 배포 후 감시 — 기록이 없으면 아직(초록 아님). 기록된 취약점 중 아직 안 고친 게 있으면 노랑.
  const watchState: StepState = w?.running ? 'running' : w?.error ? 'warn' : watchOf(w?.result);
  const rows = attackRows(input.attack);
  const attackState: StepState = input.attackRunning ? 'running' : !input.attack ? 'idle' : rows.some(r => r.state === 'found') ? 'bad' : rows.some(r => r.state !== 'blocked') ? 'warn' : 'ok';
  const steps: Step[] = [
    { key: 'gitleaks', label: '키 노출', state: stepOf('gitleaks') },
    { key: 'hadolint', label: 'Dockerfile', state: stepOf('hadolint') },
    { key: 'trivy', label: '이미지', state: stepOf('trivy') },
    { key: 'code', label: '코드', state: codeState },
    { key: 'policy', label: '배포 정책', state: policyState },
    { key: 'watch', label: '배포 후 감시', state: watchState },
    { key: 'attack', label: '공격 테스트', state: attackState },
  ];

  const gate = gateVerdict(['trivy', 'hadolint', 'gitleaks'], scans, scanning);
  const running = scanning || !!input.codeRunning || !!pol?.running;
  const basic = steps.slice(0, 4);
  const ran = basic.some(s => s.state !== 'idle');
  const unverified = basic.some(s => s.state === 'warn' && !risks.some(r => r.source === sourceOf(s.key))) || basic.some(s => s.state === 'idle');
  const verdict: Verdict = running ? 'running' : tally.danger ? 'danger' : !ran ? 'idle' : tally.caution || unverified ? 'caution' : 'safe';
  const title = verdict === 'running' ? '검사 중' : verdict === 'danger' ? '위험' : verdict === 'caution' ? '주의' : verdict === 'safe' ? '안전' : '검사 전';
  const reason = verdict === 'danger' ? '바로 확인할 위험이 있어요'
    : verdict === 'caution' ? (tally.caution ? '확인할 항목이 있어요' : '아직 확인하지 못한 검사가 있어요')
    : verdict === 'safe' ? '검사한 범위에서 문제를 찾지 못했어요' : verdict === 'running' ? '' : '';
  //: 검사 대상이 없어 건너뛴 검사 — 안전 판정에 숨기지 않고 한 줄로 알린다.
  const skipped = [scans.trivy && isNotApplicable(scans.trivy, 'trivy') ? '빌드된 이미지가 없어 이미지 검사는 건너뛰었어요' : '',
    scans.hadolint && isNotApplicable(scans.hadolint, 'hadolint') ? 'Dockerfile이 없어 Dockerfile 검사는 건너뛰었어요' : '',
    code && code.score === null ? '코드 규칙을 적용할 파일이 없었어요' : ''].filter(Boolean);
  return { verdict, title, reason, tally, risks, steps, gate, attack: rows, skipped };
}

function sourceOf(k: Step['key']): RiskSource | '' {
  return k === 'gitleaks' ? 'secret' : k === 'hadolint' ? 'dockerfile' : k === 'trivy' ? 'image' : k === 'code' ? 'code' : '';
}

function watchOf(result?: Record<string, unknown>): StepState {
  if (!result) return 'idle';
  //: recheck 응답(status·message) — 재검사할 SBOM 이 없으면 아직.
  if (typeof result.status === 'string') return result.status === 'ok' ? 'ok' : result.status === 'not_run' ? 'idle' : 'warn';
  const services = Object.values(result).filter(v => v && typeof v === 'object') as Array<{ known?: Record<string, { fixed_at?: string }> }>;
  if (!services.length) return 'idle';
  return services.some(s => Object.values(s.known ?? {}).some(v => !v.fixed_at)) ? 'warn' : 'ok';
}

/** 화면에 보내는 수정안(securityFixes.FixProposal 과 같은 모양). */
export interface FixLite { id: string; tool: string; title: string; detail: string; files: string[]; auto: boolean; rebuild?: boolean }
export type FixMatch = { kind: 'auto' | 'manual'; proposal: FixLite } | null;
const mentions = (text: string, word: string) => !!word && new RegExp(`(^|[^\\w@/.-])${word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}($|[^\\w-])`, 'i').test(text);
/**
 * 위험 한 줄에 실제로 맞는 수정안. 자동 수정안이 있을 때만 "고치기" 를 띄운다(없으면 직접 수정 안내).
 * - 이미지: 패키지 이름이 수정안에 나오거나, OS 패키지면 베이스 이미지 패치 수정안
 * - Dockerfile: 수정안 제목의 (DLxxxx) 코드
 * - 시크릿: 수정안의 파일
 */
export function fixFor(r: Risk, proposals: FixLite[]): FixMatch {
  const pick = (list: FixLite[]): FixMatch => { const a = list.find(p => p.auto); if (a) return { kind: 'auto', proposal: a }; const m = list[0]; return m ? { kind: 'manual', proposal: m } : null; };
  if (r.source === 'image') {
    //: 고친 버전이 아직 없는 취약점은 어떤 수정안으로도 사라지지 않는다 — "고치기" 를 띄우지 않는다.
    if (r.fix === 'wait') return null;
    const pkg = r.where.split(' · ')[0];
    const named = proposals.filter(p => p.tool === 'trivy' && mentions(`${p.title}\n${p.detail}`, pkg));
    if (named.length) return pick(named);
    if (r.details.some(d => d.os && d.fixed)) return pick(proposals.filter(p => p.tool === 'trivy' && p.rebuild && /베이스 이미지|OS 패키지/.test(p.title)));
    return null;
  }
  if (r.source === 'dockerfile') { const code = r.details[0]?.id; return code ? pick(proposals.filter(p => p.tool === 'hadolint' && p.title.includes(`(${code})`))) : null; }
  if (r.source === 'secret') { const file = (r.details[0]?.file ?? '').replace(/\\/g, '/'); return pick(proposals.filter(p => p.tool === 'gitleaks' && p.files.some(f => file.endsWith(f.replace(/\\/g, '/'))))); }
  return null;
}
