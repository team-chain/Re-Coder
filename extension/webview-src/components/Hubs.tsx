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
import React, { useCallback, useEffect, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

export type HubId = "develop" | "deploy" | "security";
export type FeatureId =
  | "code" | "build" | "map" | "adr"
  | "ship" | "deploy" | "replay" | "operate"
  | "scan" | "secrets" | "policy";

export interface ReadyCtx { isAiReady: boolean; isDockerReady: boolean; isOpsReady: boolean; }

interface FeatureDef {
  id: FeatureId;
  hub: HubId;
  icon: string;
  title: string;
  desc: string;
  action: string;
  primary?: boolean;
  gate?: (c: ReadyCtx) => { enabled: boolean; hint?: string };
}

export const HUBS: Array<{ id: HubId; icon: string; title: string; subtitle: string; accent: string }> = [
  { id: "develop", icon: "⌨️", title: "Develop", subtitle: "요청부터 코드 적용까지", accent: "#4a9eff" },
  { id: "deploy", icon: "🚀", title: "Deploy", subtitle: "로컬 Docker부터 AWS까지", accent: "#f0b35b" },
  { id: "security", icon: "🛡️", title: "Security", subtitle: "배포 전 검사와 정책", accent: "#ef6b6b" },
];

export const FEATURES: FeatureDef[] = [
  { id: "code", hub: "develop", icon: "✨", title: "코드 생성 · 수정", desc: "자연어로 새 코드를 만들거나 기존 코드를 고칩니다. 설계 결정 카드 → 파일별 diff → 적용 순서로 진행돼요.", action: "시작", primary: true, gate: (c) => ({ enabled: c.isAiReady, hint: "AI 연결 필요" }) },
  { id: "build", hub: "develop", icon: "🩺", title: "에러 분석", desc: "터미널·로그의 오류를 읽고 원인과 패치를 제안합니다. 승인 전에는 파일을 건드리지 않아요.", action: "분석", primary: true, gate: (c) => ({ enabled: c.isAiReady, hint: "AI 연결 필요" }) },
  { id: "map", hub: "develop", icon: "🗺️", title: "아키텍처", desc: "파일 간 의존 관계와 함수 호출 관계를 그림으로 봅니다. 코드를 실제로 읽어 만든 결과예요.", action: "보기" },
  { id: "adr", hub: "develop", icon: "📝", title: "설계 기록 (ADR)", desc: "결정 카드에서 확정한 내용이 자동으로 쌓입니다. 어떤 선택을 왜 했는지 나중에 찾아볼 수 있어요.", action: "열기" },

  { id: "ship", hub: "deploy", icon: "🐳", title: "로컬 Docker 배포", desc: "Dockerfile 생성 → 검사 → build → run → 헬스체크. 실패하면 이전 이미지로 되돌립니다.", action: "시작", primary: true, gate: (c) => ({ enabled: c.isDockerReady, hint: "Docker 필요" }) },
  { id: "deploy", hub: "deploy", icon: "☁️", title: "배포 센터", desc: "ECS · EC2 · S3 정적 사이트. 외부로 나가는 배포는 검사 결과에 따라 승인 강도가 달라져요.", action: "열기", primary: true },
  { id: "replay", hub: "deploy", icon: "↩️", title: "롤백 · Replay", desc: "배포 이력을 타임라인으로 보고 원하는 시점으로 되돌립니다.", action: "이력 보기" },
  { id: "operate", hub: "deploy", icon: "📟", title: "운영 대응", desc: "장애 감지 → 원인 분석 → 조치 제안. 실행은 승인 후에만.", action: "열기", gate: (c) => ({ enabled: c.isOpsReady, hint: "AI · AWS 연결 필요" }) },

  { id: "scan", hub: "security", icon: "🔍", title: "취약점 스캔", desc: "Trivy(이미지·의존성) · Hadolint(Dockerfile). 결과는 배포 승인 카드의 위험도에 반영됩니다.", action: "스캔", primary: true },
  { id: "secrets", hub: "security", icon: "🔑", title: "시크릿 검사", desc: "gitleaks 규칙으로 저장소에 평문 비밀값이 남았는지 확인합니다. 원문은 표시하지 않아요.", action: "검사", primary: true },
  { id: "policy", hub: "security", icon: "📜", title: "정책 게이트", desc: "어떤 배포가 자동 통과 · 승인 필요 · 차단인지 규칙(OPA)으로 봅니다.", action: "규칙 보기" },
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

// ── 허브 홈 ─────────────────────────────────────────────────────────────
export const HubHome: React.FC<{ onSelect: (hub: HubId) => void; ctx: ReadyCtx }> = ({ onSelect, ctx }) => {
  const badge = (hub: HubId): { text: string; ok: boolean } => {
    if (hub === "develop") return ctx.isAiReady ? { text: "AI 준비됨", ok: true } : { text: "AI 연결 필요", ok: false };
    if (hub === "deploy") return ctx.isDockerReady ? { text: "Docker 준비됨", ok: true } : { text: "Docker 필요", ok: false };
    return { text: "검사 · 정책", ok: true };
  };
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14, height: "100%", alignContent: "start" }}>
      {HUBS.map((h) => {
        const b = badge(h.id);
        return (
          <button key={h.id} onClick={() => onSelect(h.id)} style={{ textAlign: "left", cursor: "pointer", border: `1px solid ${C.line}`, borderTop: `3px solid ${h.accent}`, borderRadius: 14, padding: "20px 18px 16px", background: C.card, color: C.fg, display: "flex", flexDirection: "column", minHeight: 250, fontFamily: "inherit" }}>
            <div style={{ fontSize: 26, marginBottom: 12 }}>{h.icon}</div>
            <div style={{ fontSize: 19, fontWeight: 700, letterSpacing: .2 }}>{h.title}</div>
            <div style={{ color: C.muted, fontSize: 12, margin: "4px 0 14px" }}>{h.subtitle}</div>
            <ul style={{ listStyle: "none", margin: 0, padding: 0, fontSize: 12.5, flex: 1 }}>
              {FEATURES.filter((f) => f.hub === h.id).map((f, i) => (
                <li key={f.id} style={{ padding: "6px 0", borderTop: i === 0 ? "none" : `1px solid ${C.line}` }}>{f.title}</li>
              ))}
            </ul>
            <div style={{ marginTop: 12, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <span style={{ fontSize: 10.5, padding: "2px 8px", borderRadius: 999, border: `1px solid ${b.ok ? "#2f6b4a" : "#6b4a17"}`, color: b.ok ? "#7ed3a2" : "#f0b35b" }}>{b.text}</span>
              <span style={{ color: C.link, fontSize: 12, fontWeight: 600 }}>열기 ›</span>
            </div>
          </button>
        );
      })}
    </div>
  );
};

// ── 브레드크럼 + 허브 전환 ───────────────────────────────────────────────
export const HubCrumb: React.FC<{ hub: HubId; feature?: FeatureId; onHome: () => void; onHub: (h: HubId) => void }> = ({ hub, feature, onHome, onHub }) => {
  const h = HUBS.find((x) => x.id === hub)!;
  const linkBtn: React.CSSProperties = { border: "none", background: "transparent", color: C.link, cursor: "pointer", fontSize: 12.5, padding: 0, fontFamily: "inherit" };
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 14, flexWrap: "wrap" }}>
      <button onClick={onHome} style={linkBtn}>‹ 홈</button>
      <span style={{ color: C.muted }}>/</span>
      {feature ? <>
        <button onClick={() => onHub(hub)} style={linkBtn}>{h.title}</button>
        <span style={{ color: C.muted }}>/</span>
        <strong style={{ fontSize: 15 }}>{FEATURE_BY_ID[feature].title}</strong>
      </> : (
        <span style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <span style={{ fontSize: 20 }}>{h.icon}</span>
          <span><strong style={{ fontSize: 18 }}>{h.title}</strong><div style={{ color: C.muted, fontSize: 11.5 }}>{h.subtitle}</div></span>
        </span>
      )}
      <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
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
  <div>
    <HubCrumb hub={hub} onHome={onHome} onHub={onHub} />
    {banner}
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 14 }}>
      {FEATURES.filter((f) => f.hub === hub).map((f) => {
        const g = f.gate ? f.gate(ctx) : { enabled: true };
        return (
          <div key={f.id} style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card, padding: 18, display: "flex", flexDirection: "column", minHeight: 150 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 9, fontSize: 15, fontWeight: 700 }}><span style={{ fontSize: 18 }}>{f.icon}</span>{f.title}</div>
            <div style={{ color: C.muted, fontSize: 12, lineHeight: 1.5, marginTop: 8, flex: 1 }}>{f.desc}</div>
            <div style={{ marginTop: 14, display: "flex", alignItems: "center", gap: 8 }}>
              <button onClick={() => g.enabled && onOpen(f.id)} disabled={!g.enabled} style={btnStyle(!!f.primary, g.enabled)} title={!g.enabled ? g.hint : undefined}>{f.action}</button>
              {!g.enabled && g.hint && <span style={{ color: "#f0b35b", fontSize: 11 }}>{g.hint}</span>}
            </div>
          </div>
        );
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
interface ScanResultLite { scan_type: string; status?: "ok" | "error"; summary?: string; message?: string; critical_count?: number; high_count?: number; medium_count?: number; findings?: unknown; }
type ScanKind = "trivy" | "hadolint" | "gitleaks";

export const SecurityScanPanel: React.FC<{ kinds: ScanKind[]; title: string; note: string }> = ({ kinds, title, note }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [running, setRunning] = useState<ScanKind | null>(null);
  const [results, setResults] = useState<Partial<Record<ScanKind, ScanResultLite>>>({});

  useMessage(useCallback((msg) => {
    if (msg.type === "scanResult") {
      const r = msg.payload as ScanResultLite;
      const k = r.scan_type as ScanKind;
      if (kinds.includes(k)) { setResults((cur) => ({ ...cur, [k]: r })); setRunning(null); }
    } else if (msg.type === "errorMessage") {
      setRunning(null);
    }
  }, [kinds]));

  const run = (k: ScanKind) => { setRunning(k); postMessage("runScan", { scanType: k, workspacePath: "" }); };
  const label: Record<ScanKind, string> = { trivy: "Trivy (이미지 · 의존성)", hadolint: "Hadolint (Dockerfile)", gitleaks: "gitleaks (비밀값)" };

  const verdict = (r?: ScanResultLite) => {
    if (!r) return { text: "미실행", color: C.muted };
    if (r.status === "error") return { text: `검사 못 함 · ${r.message ?? "스캐너 오류"}`, color: "#f0b35b" };
    const c = r.critical_count ?? 0, h = r.high_count ?? 0;
    const n = Array.isArray(r.findings) ? r.findings.length : 0;
    if (c || h) return { text: `심각 ${c} · 높음 ${h}`, color: "#ff8b8b" };
    if (n) return { text: `발견 ${n}건 (낮음·중간)`, color: "#f0b35b" };
    return { text: "이상 없음", color: "#7ed3a2" };
  };

  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card, padding: "16px 18px" }}>
      <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 4 }}>{title}</div>
      <div style={{ color: C.muted, fontSize: 12, marginBottom: 14 }}>{note}</div>
      {kinds.map((k, i) => {
        const v = verdict(results[k]);
        return (
          <div key={k} style={{ display: "grid", gridTemplateColumns: "1fr auto auto", gap: 12, alignItems: "center", padding: "10px 0", borderTop: i === 0 ? "none" : `1px solid ${C.line}` }}>
            <div><div style={{ fontSize: 13, fontWeight: 600 }}>{label[k]}</div><div style={{ fontSize: 11.5, color: v.color, marginTop: 2 }}>{v.text}</div></div>
            <div style={{ color: C.muted, fontSize: 11 }}>{results[k]?.summary ?? ""}</div>
            <button onClick={() => run(k)} disabled={running !== null} style={btnStyle(true, running === null)}>{running === k ? "검사 중…" : "검사"}</button>
          </div>
        );
      })}
      <div style={{ marginTop: 12, color: C.muted, fontSize: 11 }}>검사를 안 한 것과 이상이 없는 것은 다르게 표시됩니다. 스캐너가 없으면 "검사 못 함"으로 남아요.</div>
    </div>
  );
};

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
