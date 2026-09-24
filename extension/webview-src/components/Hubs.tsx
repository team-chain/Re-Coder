/**
 * ReCoder — 허브 홈 / 허브 페이지 / 기능 프레임 (A안, 2026-09-17)
 *
 * 홈은 Develop · Deploy · Security 세 카드만 보여준다. 카드를 누르면 그 허브의
 * 기능 카드 페이지로, 기능을 누르면 기존 컴포넌트(BuildMode · ShipMode …)가
 * 브레드크럼 아래에 그대로 열린다. 기능 컴포넌트 자체는 건드리지 않는다.
 *
 * 왜 바꿨나: 예전 홈은 구조 지도 · Build · Deploy · 배포 센터 · Operate · 연결 ·
 * Deploy Replay 가 한 줄씩 나열돼 무엇이 무엇인지 읽기 어려웠다. 특히 "구조 지도"와
 * "전체 아키텍처 보기"는 같은 화면인데 다른 항목처럼 보였다.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { HubIcon, HubIconName } from "./HubIcons";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { useSelfHeal } from "../hooks/useSelfHeal";
import { ScanKind, ScanQueue, ScanRequest } from './scanQueue';
import { scanCounts } from './ShipMode';

export type HubId = "develop" | "deploy" | "security";
export type FeatureId =
  | "code" | "build" | "map" | "adr"
  | "ship" | "deploy" | "replay" | "operate"
  | "scan" | "secrets" | "policy";

export interface ReadyCtx { isAiReady: boolean; isDockerReady: boolean; isOpsReady: boolean; }

interface FeatureDef {
  id: FeatureId;
  hub: HubId;
  icon: HubIconName;
  title: string;
  desc: string;
  action: string;
  primary?: boolean;
  gate?: (c: ReadyCtx) => { enabled: boolean; hint?: string };
}

export const HUBS: Array<{ id: HubId; icon: HubIconName; title: string; subtitle: string; accent: string }> = [
  { id: "develop", icon: "code", title: "Develop", subtitle: "요청부터 코드 적용까지", accent: "#4a9eff" },
  { id: "deploy", icon: "rocket", title: "Deploy", subtitle: "로컬 Docker부터 AWS까지", accent: "#f0b35b" },
  { id: "security", icon: "shield", title: "Security", subtitle: "배포 전 검사와 정책", accent: "#ef6b6b" },
];

export const FEATURES: FeatureDef[] = [
  { id: "code", hub: "develop", icon: "terminal", title: "코드 생성 · 수정", desc: "요청을 입력하고, 변경 내용을 검토해 적용하세요.", action: "시작", primary: true, gate: (c) => ({ enabled: c.isAiReady, hint: "AI 연결 필요" }) },
  { id: "build", hub: "develop", icon: "bug", title: "에러 분석", desc: "오류의 원인과 수정안을 확인하세요.", action: "분석", gate: (c) => ({ enabled: c.isAiReady, hint: "AI 연결 필요" }) },
  { id: "map", hub: "develop", icon: "network", title: "아키텍처", desc: "파일과 함수가 어떻게 연결되는지 살펴보세요.", action: "보기" },
  { id: "adr", hub: "develop", icon: "file-text", title: "설계 기록 (ADR)", desc: "이전에 결정한 설계와 선택 이유를 찾아보세요.", action: "열기" },

  { id: "ship", hub: "deploy", icon: "box", title: "로컬 Docker 배포", desc: "Dockerfile 생성 → 검사 → build → run → 헬스체크. 실패하면 이전 이미지로 되돌립니다.", action: "시작", primary: true, gate: (c) => ({ enabled: c.isDockerReady, hint: "Docker 필요" }) },
  { id: "deploy", hub: "deploy", icon: "cloud-upload", title: "배포 캔버스", desc: "프로젝트를 Docker · GitHub · ECS · S3로 연결하고, 승인과 보안 검사부터 실행 상태까지 확인합니다.", action: "열기", primary: true },
  { id: "replay", hub: "deploy", icon: "history", title: "롤백 · Replay", desc: "저장된 배포·롤백 이력을 보고 이전 버전 복귀 대상을 확인합니다.", action: "이력 보기" },
  { id: "operate", hub: "deploy", icon: "activity", title: "운영 대응", desc: "장애 감지 → 원인 분석 → 조치 제안. 실행은 승인 후에만.", action: "열기", gate: (c) => ({ enabled: c.isOpsReady, hint: "AI · AWS 연결 필요" }) },

  { id: "scan", hub: "security", icon: "search", title: "취약점 스캔", desc: "이미지·의존성과 Dockerfile의 문제를 확인하세요.", action: "스캔", primary: true },
  { id: "secrets", hub: "security", icon: "key", title: "시크릿 검사", desc: "소스에 남은 API 키와 비밀번호를 검사하세요.", action: "검사" },
  { id: "policy", hub: "security", icon: "clipboard-check", title: "정책 게이트", desc: "배포 승인과 차단 기준을 확인하세요.", action: "규칙 보기" },
];

export const FEATURE_BY_ID: Record<FeatureId, FeatureDef> = FEATURES.reduce((acc, f) => { acc[f.id] = f; return acc; }, {} as Record<FeatureId, FeatureDef>);
export const hubOf = (f: FeatureId): HubId => FEATURE_BY_ID[f].hub;
export const isHubView = (v: string): v is `hub:${HubId}` => v.startsWith("hub:");
export const isFeatureView = (v: string): v is FeatureId => (FEATURES as Array<{ id: string }>).some((f) => f.id === v);

// ── 스타일 토큰 ──────────────────────────────────────────────────────────
const C = {
  fg: "var(--vscode-foreground, #e0e0e0)",
  muted: "var(--vscode-descriptionForeground, #8f98a3)",
  line: "var(--vscode-panel-border, #2e3238)",
  card: "var(--vscode-editorWidget-background, #202326)",
  link: "var(--vscode-textLink-foreground, #4a9eff)",
  btn: "var(--vscode-button-background, #0e639c)",
  btnFg: "var(--vscode-button-foreground, #fff)",
};
const btnStyle = (primary: boolean, enabled = true): React.CSSProperties => ({
  border: primary ? "1px solid transparent" : `1px solid ${C.line}`,
  borderRadius: 6, padding: "6px 12px", fontSize: 11.5, fontWeight: 600, cursor: enabled ? "pointer" : "default",
  background: primary ? C.btn : "transparent", color: primary ? C.btnFg : C.fg, opacity: enabled ? 1 : 0.5,
});

const DockerFix: React.FC = () => {
  const { heal, start } = useSelfHeal();
  const state = heal.docker_ready;
  return <div style={{ fontSize: 11, color: C.muted }}>
    <button disabled={!!state?.pending} onClick={() => start("docker_ready")}
      style={{ ...btnStyle(false, !state?.pending), color: "#f0b35b" }}>
      {state?.pending ? "조치 중…" : "Docker 필요 · 자동 조치"}
    </button>
    {state?.message && <div role="status" style={{ marginTop: 5, maxWidth: 260, lineHeight: 1.5, color: state.failed ? "#f0b35b" : C.muted }}>{state.message}</div>}
  </div>;
};

// ── 허브 홈 ─────────────────────────────────────────────────────────────
export const HubHome: React.FC<{ onSelect: (hub: HubId) => void; ctx: ReadyCtx }> = ({ onSelect }) => {
  return (
    <section className="rc-home" aria-label="시작하기">
      <style>{`
        .rc-home{max-width:920px;margin:clamp(16px,7vh,72px) auto;container-type:inline-size}
        .rc-home h1{font-size:24px;font-weight:600;letter-spacing:-.5px;margin:0 0 10px}
        .rc-home-intro{color:${C.muted};font-size:13px;margin:0 0 30px}
        .rc-home-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
        @container(max-width:640px){.rc-home-grid{grid-template-columns:1fr}.rc-home-card{min-height:120px!important;padding:20px!important}}
        .rc-home-card{display:flex;flex-direction:column;align-items:flex-start;gap:10px;text-align:left;border:1px solid ${C.line};border-radius:10px;padding:24px;min-height:170px;background:transparent;color:${C.fg};font:inherit;cursor:pointer}
        .rc-home-card:hover{background:${C.card};border-color:var(--vscode-focusBorder,#587fa1)}
        .rc-home-card strong{font-size:17px;font-weight:600;margin-top:9px}.rc-home-card small{font-size:12px;color:${C.muted}}
        .rc-home-card:focus-visible{outline:2px solid var(--vscode-focusBorder,#75b9ef);outline-offset:3px}
      `}</style>
      <h1>무엇을 할까요?</h1>
      <p className="rc-home-intro">지금 할 작업을 선택하세요.</p>
      <div className="rc-home-grid">{HUBS.map(h => <button key={h.id} className="rc-home-card" onClick={() => onSelect(h.id)}>
        <span style={{ color: h.accent }}><HubIcon name={h.icon} size={24} /></span>
        <strong>{h.title}</strong><small>{h.subtitle}</small>
      </button>)}</div>
    </section>
  );
};

// ── 브레드크럼 + 허브 전환 ───────────────────────────────────────────────
export const HubCrumb: React.FC<{ hub: HubId; feature?: FeatureId; onHome: () => void; onHub: (h: HubId) => void }> = ({ hub, feature, onHome, onHub }) => {
  const h = HUBS.find((x) => x.id === hub)!;
  const linkBtn: React.CSSProperties = { border: "none", background: "transparent", color: C.link, cursor: "pointer", fontSize: 12.5, padding: 0, fontFamily: "inherit" };
  return (
    <div className="rc-hub-crumb" style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 14, flexWrap: "wrap" }}>
      <button onClick={onHome} style={linkBtn}>‹ 홈</button>
      <span style={{ color: C.muted }}>/</span>
      {feature ? <>
        <button onClick={() => onHub(hub)} style={linkBtn}>{h.title}</button>
        <span style={{ color: C.muted }}>/</span>
        <strong style={{ fontSize: 15 }}>{FEATURE_BY_ID[feature].title}</strong>
      </> : (
        <span style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <strong style={{ fontSize: 15 }}>{h.title}</strong>
        </span>
      )}
      <div className="rc-local-hub-nav" style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
        {HUBS.map((x) => (
          <button key={x.id} onClick={() => onHub(x.id)} style={{ padding: "4px 10px", borderRadius: 6, fontSize: 11, cursor: "pointer", fontFamily: "inherit",
            border: `1px solid ${x.id === hub ? "#3f7fb5" : C.line}`, background: x.id === hub ? "#1c2b3d" : "transparent", color: x.id === hub ? "#fff" : C.muted }}>{x.title}</button>
        ))}
      </div>
    </div>
  );
};

// ── 허브 페이지 (기능 카드) ──────────────────────────────────────────────
export const HubPage: React.FC<{ hub: HubId; ctx: ReadyCtx; onOpen: (f: FeatureId) => void; onHome: () => void; onHub: (h: HubId) => void; banner?: React.ReactNode }> = ({ hub, ctx, onOpen, onHome, onHub, banner }) => (
  <div className="rc-hub-page">
    <style>{`
      .rc-hub-page{max-width:860px;margin:20px auto;container-type:inline-size}
      .rc-feature-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:32px}
      .rc-feature-card{position:relative;display:flex;flex-direction:column;align-items:flex-start;gap:13px;min-height:158px;padding:24px 46px 24px 24px;border:1px solid ${C.line};border-radius:12px;background:transparent;color:${C.fg};text-align:left;font:inherit;cursor:pointer;transition:border-color .15s,background .15s}
      .rc-feature-card[data-primary=true]{background:color-mix(in srgb,${C.link} 5%,transparent);border-color:color-mix(in srgb,${C.link} 35%,${C.line})}
      .rc-feature-card:hover:not(:disabled){border-color:${C.link};background:color-mix(in srgb,${C.link} 8%,transparent)}
      .rc-feature-card strong{display:flex;align-items:center;gap:10px;font-size:15px;font-weight:600}
      .rc-feature-card small{color:${C.muted};font-size:12px;line-height:1.7}
      .rc-feature-card .rc-card-arrow{position:absolute;right:23px;top:26px;color:${C.muted};font-size:18px}
      .rc-feature-card:disabled{cursor:default;opacity:.6}.rc-feature-card:focus-visible{outline:2px solid ${C.link};outline-offset:3px}
      .rc-feature-hint{color:#f0b35b;font-size:11px}
      @container(max-width:520px){.rc-feature-grid{grid-template-columns:1fr;gap:12px;margin-top:24px}.rc-feature-card{min-height:130px;padding:20px 42px 20px 20px}}
    `}</style>
    <HubCrumb hub={hub} onHome={onHome} onHub={onHub} />
    {banner}
    <div className="rc-feature-grid">
      {FEATURES.filter((f) => f.hub === hub).map((f) => {
        const g = f.gate ? f.gate(ctx) : { enabled: true };
        const content = <><strong><span style={{ color: HUBS.find(x => x.id === f.hub)!.accent }}><HubIcon name={f.icon} size={19} /></span>{f.title}</strong><small>{f.desc}</small><span className="rc-card-arrow" aria-hidden="true">↗</span></>;
        if (f.id === 'ship' && !g.enabled) return <div key={f.id} className="rc-feature-card">{content}<DockerFix /></div>;
        return <button key={f.id} className="rc-feature-card" data-primary={Boolean(f.primary)} disabled={!g.enabled} title={g.enabled ? undefined : g.hint} onClick={() => onOpen(f.id)}>{content}{!g.enabled && <span className="rc-feature-hint">{g.hint}</span>}</button>;
      })}
    </div>
  </div>
);

// ── 기능 프레임 (브레드크럼 + 기존 컴포넌트) ─────────────────────────────
export const FeatureFrame: React.FC<{ feature: FeatureId; onHome: () => void; onHub: (h: HubId) => void; children: React.ReactNode }> = ({ feature, onHome, onHub, children }) => (
  <div>
    <HubCrumb hub={hubOf(feature)} feature={feature} onHome={onHome} onHub={onHub} />
    {children}
  </div>
);

// ── ADR 목록/본문 ─────────────────────────────────────────────────────────
interface AdrItem { file: string; title: string; id: string; }

export const AdrPanel: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [items, setItems] = useState<AdrItem[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [content, setContent] = useState<string>("");
  const [error, setError] = useState<string>("");

  useEffect(() => { postMessage("adr.list", {}); }, [postMessage]);
  useMessage(useCallback((msg) => {
    if (msg.type === "adr.listResult") {
      const p = msg.payload as { items?: AdrItem[]; error?: string };
      setItems(p.items ?? []); setError(p.error ?? "");
      if ((p.items ?? []).length && !selected) { const first = p.items![0].file; setSelected(first); postMessage("adr.read", { file: first }); }
    } else if (msg.type === "adr.readResult") {
      const p = msg.payload as { file?: string; content?: string; error?: string };
      if (p.file === selected) { setContent(p.content ?? ""); setError(p.error ?? ""); }
    }
  }, [postMessage, selected]));

  const open = (file: string) => { setSelected(file); setContent(""); postMessage("adr.read", { file }); };
  const meta = (() => {
    const m = /(?:생성|Generated by|모델|model)[^\n]*/i.exec(content);
    return m ? m[0] : "";
  })();
  const reviewed = /검토자\s*[:：]\s*\S+/.test(content) && !/검토자\s*[:：]\s*(\(|미검토|TBD|-)/.test(content);

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(200px, 240px) 1fr", gap: 14 }}>
      <div style={{ border: `1px solid ${C.line}`, borderRadius: 10, background: C.card, overflow: "hidden", alignSelf: "start" }}>
        {items.length === 0 && <div style={{ padding: 12, color: C.muted, fontSize: 12 }}>{error || "docs/adr 에 기록이 없어요. 코드 생성에서 설계 결정을 확정하면 여기에 쌓입니다."}</div>}
        {items.map((it, i) => (
          <button key={it.file} onClick={() => open(it.file)} style={{ display: "block", width: "100%", textAlign: "left", padding: "10px 12px", borderTop: i === 0 ? "none" : `1px solid ${C.line}`, borderLeft: "none", borderRight: "none", borderBottom: "none", background: it.file === selected ? "#1c2b3d" : "transparent", color: C.fg, cursor: "pointer", fontFamily: "inherit", fontSize: 12 }}>
            <div style={{ color: "#8fa4b5", fontFamily: "var(--vscode-editor-font-family, Menlo, monospace)", fontSize: 10.5 }}>{it.id}</div>
            <div>{it.title}</div>
          </button>
        ))}
      </div>
      <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card, padding: "16px 18px", minHeight: 200 }}>
        {selected ? <>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
            <strong style={{ fontSize: 14 }}>{items.find((i) => i.file === selected)?.title ?? selected}</strong>
            <span style={{ fontSize: 10.5, padding: "2px 7px", borderRadius: 999, border: `1px solid ${reviewed ? "#2f6b4a" : "#6b4a17"}`, color: reviewed ? "#7ed3a2" : "#f0b35b" }}>{reviewed ? "검토됨" : "미검토"}</span>
            {meta && <span style={{ color: C.muted, fontSize: 11 }}>{meta}</span>}
            <button onClick={() => postMessage("adr.open", { file: selected })} style={{ ...btnStyle(false), marginLeft: "auto" }}>에디터에서 열기</button>
          </div>
          <pre style={{ margin: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: "inherit", fontSize: 12, lineHeight: 1.6, color: C.fg }}>{content || (error ? error : "불러오는 중…")}</pre>
        </> : <div style={{ color: C.muted, fontSize: 12 }}>왼쪽에서 기록을 선택하세요.</div>}
      </div>
    </div>
  );
};

// ── 보안: 스캔 / 시크릿 / 정책 ────────────────────────────────────────────
interface ScanResultLite { requestId?: string; scan_type: string; status?: "ok" | "error" | "not_run" | "unverified"; summary?: string; message?: string; cause?: string; next_action?: string; reason_code?: string; critical_count?: number; high_count?: number; medium_count?: number; findings?: unknown; }

export function securityResultDetail(r?: ScanResultLite): string {
  if (!r) return '아직 검사 결과가 없습니다.';
  const cause = r.cause ?? r.summary ?? r.message ?? '검사 결과를 확인하지 못했습니다.';
  return `${cause}${r.next_action ? `\n다음 행동: ${r.next_action}` : ''}`;
}

export const SecurityScanPanel: React.FC<{ kinds: ScanKind[]; title: string; note: string }> = ({ kinds, title, note }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [running, setRunning] = useState<ScanKind | null>(null);
  const [results, setResults] = useState<Partial<Record<ScanKind, ScanResultLite>>>({});
  const queue = useRef(new ScanQueue());
  const timeout = useRef<ReturnType<typeof setTimeout>>();
  const send = useCallback((request: ScanRequest | null) => {
    clearTimeout(timeout.current);
    setRunning(request?.scanType ?? null);
    if (!request) return;
    postMessage('runScan', request);
    timeout.current = setTimeout(() => {
      if (queue.current.current?.requestId !== request.requestId) return;
      queue.current.stop(); setRunning(null);
      setResults(cur => ({ ...cur, [request.scanType]: { scan_type: request.scanType, status: 'unverified', cause: '검사 응답을 받지 못했습니다.', next_action: '연결 상태를 확인한 뒤 다시 검사하세요.' } }));
    }, 330000);
  }, [postMessage]);
  useEffect(() => () => { clearTimeout(timeout.current); queue.current.stop(); }, []);

  useMessage(useCallback((msg) => {
    if (msg.type === "scanResult") {
      const r = msg.payload as ScanResultLite;
      const k = r.scan_type as ScanKind;
      if (queue.current.matches(r)) { setResults((cur) => ({ ...cur, [k]: r })); send(queue.current.complete()); }
    }
  }, [send]));

  const run = (selected: ScanKind[]) => {
    const request = queue.current.start(selected);
    if (!request) return;
    setResults(cur => { const next = { ...cur }; selected.forEach(k => { delete next[k]; }); return next; });
    send(request);
  };
  const label: Record<ScanKind, string> = { trivy: '이미지 · 의존성', hadolint: 'Dockerfile', gitleaks: '시크릿' };

  const verdict = (r?: ScanResultLite) => {
    if (!r) return { text: "미실행", color: C.muted };
    if (r.status !== "ok") {
      //: raw 메시지가 아니라 코어가 분류한 원인 → 다음 행동.
      return { text: '검사 미확인', color: "#f0b35b" };
    }
    const counts = scanCounts({ ...r, exit_code:0, findings:r.findings });
    if (!counts) return { text:'검사 미확인', color:'#f0b35b' };
    const c = counts.critical, h = counts.high;
    const n = Math.max(Array.isArray(r.findings) ? r.findings.length : 0, r.medium_count ?? 0);
    if (c || h) return { text: `심각 ${c} · 높음 ${h}`, color: "#ff8b8b" };
    if (n) return { text: `발견 ${n}건 (낮음·중간)`, color: "#f0b35b" };
    return { text: "이상 없음", color: "#7ed3a2" };
  };

  return (
    <div className="rc-scan-panel" aria-busy={running !== null}>
      <style>{`
        .rc-scan-panel{border:1px solid ${C.line};border-radius:12px;padding:24px;background:transparent}
        .rc-scan-heading{display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap;margin-bottom:24px}
        .rc-scan-heading h2{font-size:17px;font-weight:600;margin:0 0 8px}.rc-scan-heading p{font-size:12px;color:${C.muted};line-height:1.6;margin:0;max-width:480px}
        .rc-scan-row{border-top:1px solid ${C.line}}.rc-scan-row summary{cursor:pointer;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:20px 0;list-style:none;font-size:13px}.rc-scan-row summary::-webkit-details-marker{display:none}
        .rc-scan-row summary span{font-size:11px}.rc-scan-row summary span::after{content:' ＋';color:${C.muted}}.rc-scan-row[open] summary span::after{content:' −'}
        .rc-scan-detail{font-size:12px;line-height:1.7;color:${C.muted};padding:0 0 20px}.rc-scan-detail p{margin:0 0 12px;overflow-wrap:anywhere}
        .rc-security-page{max-width:860px;margin:20px auto}.rc-security-policy{margin-top:22px}.rc-security-policy>summary{cursor:pointer;color:${C.muted};font-size:12px;padding:12px 0}
      `}</style>
      <div className="rc-scan-heading"><div><h2>{title}</h2><p>{note}</p></div><button onClick={() => run(kinds)} disabled={running !== null} style={{ ...btnStyle(true, running === null), padding:'10px 16px' }}>{running ? '검사 중…' : '보안 검사 시작'}</button></div>
      {kinds.map(k => {
        const v = verdict(results[k]);
        return (
          <details key={k} className="rc-scan-row">
            <summary><strong>{label[k]}</strong><span style={{ color: running === k ? C.link : v.color }}>{running === k ? '검사 중…' : v.text}</span></summary>
            <div className="rc-scan-detail"><p style={{ whiteSpace:'pre-line' }}>{k} · {securityResultDetail(results[k])}</p><button onClick={() => run([k])} disabled={running !== null} style={btnStyle(false, running === null)}>{label[k]}만 검사</button></div>
          </details>
        );
      })}
      <div role="status" style={{ marginTop: 12, color: C.muted, fontSize: 11 }}>{running ? `${label[running]} 검사 중` : '항목을 누르면 결과와 개별 검사 메뉴가 열립니다.'}</div>
    </div>
  );
};

export const SecurityHub: React.FC<{ onHome: () => void; onHub: (hub: HubId) => void }> = ({ onHome, onHub }) => <section className="rc-security-page">
  <HubCrumb hub="security" onHome={onHome} onHub={onHub} />
  <div style={{ marginTop:32 }}><SecurityScanPanel kinds={['trivy','hadolint','gitleaks']} title="배포 전 확인" note="이미지, Dockerfile, 소스에 남은 비밀값을 한 번에 검사하세요." /></div>
  <details className="rc-security-policy"><summary>배포 승인 · 차단 기준 보기</summary><PolicyPanel /></details>
</section>;

export const PolicyPanel: React.FC = () => {
  const { postMessage } = useVSCodeApi();
  const rows: Array<[string, string, string]> = [
    ["로컬 Docker (본인 PC)", "1인 승인", "검사 미실행이면 위험도만 올리고 승인 단계는 유지"],
    ["ECS · EC2 (외부 접속)", "1인 승인 → 미검증 시 2인", "심각 취약점 발견 시 차단"],
    ["S3 정적 사이트", "1인 승인", "비밀 파일(.env 등) 포함 시 차단"],
    ["롤백", "1인 승인", "이전 성공 배포로만"],
  ];
  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card, padding: "16px 18px" }}>
      <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 4 }}>배포 정책 게이트</div>
      <div style={{ color: C.muted, fontSize: 12, marginBottom: 14 }}>배포 종류별로 어떤 승인이 필요하고 무엇이 차단되는지. 규칙 원문은 policies/recoder/deploy.rego 에 있어요.</div>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead><tr style={{ color: C.muted, textAlign: "left" }}><th style={{ padding: "6px 0", fontWeight: 500 }}>대상</th><th style={{ padding: "6px 0", fontWeight: 500 }}>승인</th><th style={{ padding: "6px 0", fontWeight: 500 }}>차단 · 상향 조건</th></tr></thead>
        <tbody>{rows.map((r) => <tr key={r[0]} style={{ borderTop: `1px solid ${C.line}` }}><td style={{ padding: "8px 0", fontWeight: 600 }}>{r[0]}</td><td style={{ padding: "8px 8px 8px 0" }}>{r[1]}</td><td style={{ padding: "8px 0", color: C.muted }}>{r[2]}</td></tr>)}</tbody>
      </table>
      <div style={{ marginTop: 14, display: "flex", gap: 8 }}>
        <button onClick={() => postMessage("map.openFile", { id: "policies/recoder/deploy.rego" })} style={btnStyle(false)}>규칙 원문 열기</button>
      </div>
    </div>
  );
};
