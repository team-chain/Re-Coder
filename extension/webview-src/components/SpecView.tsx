/**
 * 명세 — 요구사항 · 설계 · 작업 (4.0.1)
 *
 * 실사용 2026-11-19: 글이 많고, 이모티콘 · 반짝이 아이콘 · 색 배지가 "AI 가 만든 화면" 처럼 보인다는 의견.
 * GitHub(Checks · Issues) · Vercel(Deployments) · Linear 의 표현을 따른다:
 *   무채색 바탕, 상태 색은 아이콘에만, 밑줄 탭 + 회색 숫자, 표 형태의 목록, 텍스트 버튼.
 * 데이터는 Core /api/project/spec. AI 검토는 버튼을 누를 때만.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import SystemFlow from "./SystemFlow";
import type { FlowGraph } from "./dryRunModel";
import { runSpec, SpecRun } from "./specDryRun";

export type Status = "ok" | "fail" | "missing" | "warn" | "partial" | "unknown";
export interface SpecCriterion { cond: string; text: string; status: Status; source: "test" | "ai"; api?: string; test?: string; got?: number; note?: string }
export interface SpecApi {
  service: string; method: string; path: string; auth: boolean; admin: boolean; public: boolean;
  implemented: boolean; file: string; line: number; tests: { id: string; status: Status }[];
}
export interface SpecFeature {
  key: string; label: string; internal: boolean; services: string[]; status: Status;
  endpoints: SpecApi[]; criteria: SpecCriterion[]; counts: { api: number; criteria: number; ok: number; fail: number };
}
export interface SpecTask { id: string; title: string; detail: string; severity: "high" | "medium" | "low"; feature: string; source: "rule" | "ai" }
export interface SpecService { name: string; path?: string; kind: string; language: string; port: number; public: boolean; databases: string[]; depends_on: string[]; purpose: string; api: number }
export interface Spec {
  available: boolean; reason?: string; project?: string;
  features?: SpecFeature[]; services?: SpecService[]; databases?: { name: string; engine: string; tables: string[] }[];
  endpoints?: SpecApi[]; tasks?: SpecTask[];
  tests?: { passed: number; total: number; at: string; status: string };
  review?: { at: string; summary: string; fresh: boolean; missing: string[] } | null;
  deploy?: null | { id: string; at: string; status: string; failures: string[]; url: string; services: Record<string, "ok" | "fail" | "unknown"> };
}
export const STATUS_LABEL: Record<Status, string> = { ok: "통과", fail: "실패", missing: "없음", warn: "확인 필요", partial: "일부 확인", unknown: "확인 전" };

export const SPEC_CSS = `
.rc-spec{--sp-line:var(--rc-line2,rgba(160,190,220,.12));--sp-hover:rgba(148,178,210,.045);--sp-sel:rgba(148,178,210,.08);
  --sp-ok:#3fb950;--sp-bad:#f85149;--sp-warn:#d29922;--sp-fg:var(--rc-fg,#e6edf3);--sp-fg2:var(--rc-fg2,#9aa8b8);--sp-mute:var(--rc-fg3,#6e7b8b);
  --sp-mono:var(--vscode-editor-font-family,var(--rc-mono,ui-monospace,Menlo,Consolas,monospace));
  border:1px solid var(--sp-line);border-radius:8px;background:var(--rc-solid,#0d1117);color:var(--sp-fg);font-size:13px;line-height:1.45;overflow:hidden}
.rc-spec *{box-sizing:border-box}
.rc-spec button{font:inherit;color:inherit}
.sp-head{display:flex;align-items:center;gap:16px;padding:0 16px;border-bottom:1px solid var(--sp-line)}
.sp-name{font-weight:600;font-size:14px;padding:14px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px}
.sp-tabs{display:flex;gap:4px;align-self:stretch;margin-left:8px}
.sp-tabs button{all:unset;cursor:pointer;display:flex;align-items:center;gap:6px;padding:0 10px;font-size:13px;color:var(--sp-fg2);border-bottom:2px solid transparent;margin-bottom:-1px}
.sp-tabs button:hover{color:var(--sp-fg)}
.sp-tabs button[aria-selected=true]{color:var(--sp-fg);font-weight:500;border-bottom-color:var(--rc-teal,#2f81f7)}
.sp-count{display:inline-block;min-width:18px;padding:0 6px;border-radius:999px;background:rgba(148,178,210,.14);color:var(--sp-fg2);font-size:11px;font-weight:500;line-height:18px;text-align:center}
.sp-right{margin-left:auto;display:flex;align-items:center;gap:12px}
.sp-checks{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--sp-fg2);white-space:nowrap}
.sp-btn{all:unset;box-sizing:border-box;cursor:pointer;height:28px;padding:0 12px;display:inline-flex;align-items:center;gap:6px;border-radius:6px;border:1px solid var(--sp-line);background:rgba(148,178,210,.05);font-size:12px;font-weight:500;color:var(--sp-fg);white-space:nowrap}
.sp-btn:hover{background:rgba(148,178,210,.1)}.sp-btn[aria-disabled=true]{opacity:.55;cursor:default}
.sp-ic{flex:none;display:inline-block;vertical-align:middle}
.sp-ic.ok{color:var(--sp-ok)}.sp-ic.fail{color:var(--sp-bad)}.sp-ic.missing,.sp-ic.warn,.sp-ic.partial{color:var(--sp-warn)}.sp-ic.unknown{color:var(--sp-mute)}
.sp-main{display:grid;grid-template-columns:232px minmax(0,1fr);min-height:440px}
.sp-side{border-right:1px solid var(--sp-line);padding:8px}
.sp-item{all:unset;box-sizing:border-box;cursor:pointer;display:flex;align-items:center;gap:8px;width:100%;padding:6px 8px;border-radius:6px;color:var(--sp-fg2)}
.sp-item:hover{background:var(--sp-hover);color:var(--sp-fg)}
.sp-item[aria-current=true]{background:var(--sp-sel);color:var(--sp-fg);font-weight:500}
.sp-item .lb{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sp-item .n{color:var(--sp-mute);font-size:12px;font-variant-numeric:tabular-nums;font-weight:400}
.sp-doc{padding:20px 24px 24px;min-width:0}
.sp-doc h2{margin:0 0 2px;font-size:15px;font-weight:600}
.sp-meta{margin:0 0 16px;font-size:12px;color:var(--sp-mute)}
.sp-sec{font-size:12px;font-weight:600;color:var(--sp-fg2);margin:20px 0 8px}
.sp-sec:first-of-type{margin-top:0}
.sp-list{border:1px solid var(--sp-line);border-radius:6px}
.sp-li{display:flex;align-items:center;gap:10px;padding:8px 12px;border-top:1px solid var(--sp-line)}
.sp-li:first-child{border-top:0}
.sp-li .tx{flex:1;min-width:0}
.sp-api{all:unset;box-sizing:border-box;cursor:pointer;display:grid;grid-template-columns:52px minmax(0,1fr) 56px 16px;gap:12px;align-items:center;width:100%;padding:7px 12px;border-top:1px solid var(--sp-line)}
.sp-api:first-child{border-top:0}.sp-api:hover{background:var(--sp-hover)}
.sp-m{font:600 11px var(--sp-mono);letter-spacing:.02em;color:var(--sp-fg2)}
.sp-p{font:12.5px var(--sp-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sp-au{font-size:12px;color:var(--sp-mute);text-align:right}
.sp-more{padding:8px 12px 10px 76px;font-size:12px;color:var(--sp-fg2);border-top:1px solid var(--sp-line);background:rgba(148,178,210,.025);display:grid;gap:6px}
.sp-more .r{display:flex;align-items:center;gap:8px}
.sp-link{all:unset;cursor:pointer;color:var(--rc-teal,#2f81f7);font:12px var(--sp-mono)}.sp-link:hover{text-decoration:underline}
.sp-note{font-size:12px;color:var(--sp-mute)}
.sp-pad{padding:20px 24px 24px}
.sp-sev{font-size:11px;font-weight:500;padding:0 7px;border-radius:999px;border:1px solid var(--sp-line);color:var(--sp-fg2);line-height:18px;white-space:nowrap}
.sp-sev.high{color:var(--sp-bad);border-color:rgba(248,81,73,.4)}
.sp-sev.medium{color:var(--sp-warn);border-color:rgba(210,153,34,.4)}
.sp-sub{display:block;font-size:12px;color:var(--sp-mute);margin-top:1px}
.sp-dry-bar{display:flex;align-items:center;gap:16px;padding:12px 24px;border-bottom:1px solid var(--sp-line);font-size:12.5px;color:var(--sp-fg2)}
.sp-dry-bar b{color:var(--sp-fg);font-weight:600;font-variant-numeric:tabular-nums}
.sp-dry-bar .st{display:flex;align-items:center;gap:6px}
.sp-dry-bar .sp-btn{margin-left:auto}
.sp-empty{padding:48px 20px;text-align:center;color:var(--sp-mute);font-size:13px}
.sp-err{margin:12px 16px 0;padding:8px 12px;border-radius:6px;font-size:12px;border:1px solid rgba(248,81,73,.4);color:var(--sp-bad)}
@media(max-width:760px){.sp-main{grid-template-columns:1fr}.sp-side{border-right:0;border-bottom:1px solid var(--sp-line);display:flex;overflow-x:auto;gap:2px}.sp-item{width:auto;white-space:nowrap}
  .sp-head{flex-wrap:wrap;gap:8px}.sp-tabs{margin-left:0;min-height:36px}.sp-right{margin-left:0;padding-bottom:8px}.sp-api{grid-template-columns:48px minmax(0,1fr) 16px}.sp-au{display:none}}
`;

// ── 아이콘 (Octicons 와 같은 16px 선 굵기 · 모양) ───────────────────────
const Ic: React.FC<{ s: Status; size?: number }> = ({ s, size = 14 }) => {
  const common = { className: `sp-ic ${s}`, width: size, height: size, viewBox: "0 0 16 16", "aria-label": STATUS_LABEL[s], role: "img" } as const;
  if (s === "ok") return <svg {...common} fill="currentColor"><title>{STATUS_LABEL[s]}</title><path d="M8 16A8 8 0 1 1 8 0a8 8 0 0 1 0 16Zm3.78-9.72a.75.75 0 0 0-1.06-1.06L6.75 9.19 5.28 7.72a.75.75 0 0 0-1.06 1.06l2 2a.75.75 0 0 0 1.06 0Z" /></svg>;
  if (s === "fail") return <svg {...common} fill="currentColor"><title>{STATUS_LABEL[s]}</title><path d="M2.343 13.657A8 8 0 1 1 13.658 2.343 8 8 0 0 1 2.343 13.657ZM6.03 4.97a.75.75 0 0 0-1.06 1.06L6.94 8 4.97 9.97a.75.75 0 1 0 1.06 1.06L8 9.06l1.97 1.97a.75.75 0 1 0 1.06-1.06L9.06 8l1.97-1.97a.75.75 0 1 0-1.06-1.06L8 6.94Z" /></svg>;
  if (s === "missing" || s === "warn" || s === "partial") return <svg {...common} fill="currentColor"><title>{STATUS_LABEL[s]}</title><path d="M6.457 1.047c.659-1.234 2.427-1.234 3.086 0l6.082 11.378A1.75 1.75 0 0 1 14.082 15H1.918a1.75 1.75 0 0 1-1.543-2.575Zm1.763.707a.25.25 0 0 0-.44 0L1.698 13.132a.25.25 0 0 0 .22.368h12.164a.25.25 0 0 0 .22-.368Zm.53 3.996v2.5a.75.75 0 0 1-1.5 0v-2.5a.75.75 0 0 1 1.5 0ZM9 11a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z" /></svg>;
  return <svg {...common} fill="none" stroke="currentColor" strokeWidth="1.5"><title>{STATUS_LABEL[s]}</title><circle cx="8" cy="8" r="6.25" strokeDasharray="2.5 2.2" /></svg>;
};

const testBrief = (c: SpecCriterion): string =>
  c.text.startsWith("로그인하지") ? "로그인 없이 호출하면 거절" : c.text.startsWith("요청을 받으면") ? "서버 오류 없이 응답"
    : c.text.replace(/^올바른 요청에 /, "").replace(/ 로 응답한다$/, " 응답").replace(/한다$/, "");

function apiStatus(a: SpecApi): Status {
  if (!a.implemented) return "fail";
  if (a.tests.some(t => t.status === "fail")) return "fail";
  if (a.tests.length && a.tests.every(t => t.status === "ok")) return "ok";
  return "unknown";
}
const authText = (a: SpecApi) => a.admin ? "관리자" : a.auth ? "로그인" : "공개";

const ApiRow: React.FC<{ a: SpecApi; criteria: SpecCriterion[]; open: boolean; onToggle: () => void; onOpenFile: (f: string, l: number) => void }> = ({ a, criteria, open, onToggle, onOpenFile }) => {
  const mine = criteria.filter(c => c.source === "test" && c.api === `${a.method} ${a.path}`);
  return <>
    <button type="button" className="sp-api" aria-expanded={open} onClick={onToggle}>
      <span className="sp-m">{a.method}</span><span className="sp-p" title={a.path}>{a.path}</span>
      <span className="sp-au">{authText(a)}</span><Ic s={apiStatus(a)} />
    </button>
    {open && <div className="sp-more">
      <div className="r">{a.implemented
        ? <button type="button" className="sp-link" onClick={() => onOpenFile(a.file, a.line)}>{a.file}:{a.line}</button>
        : <><Ic s="fail" size={12} />코드에서 찾지 못함</>}</div>
      {mine.map(c => <div className="r" key={c.test}><Ic s={c.status} size={12} />{testBrief(c)}{c.got !== undefined && c.status === "fail" ? ` (실제 ${c.got})` : ""}</div>)}
    </div>}
  </>;
};

// ── 화면 ─────────────────────────────────────────────────────────────────
type Tab = "dry" | "req" | "tasks";
export const SpecView: React.FC<{ initial?: Spec | null; initialTab?: Tab; onStartTask?: (task: SpecTask) => void; onOpenFile?: (file: string, line: number) => void; onOpenStructure?: () => void }> = ({ initial = null, initialTab = "dry", onStartTask, onOpenFile, onOpenStructure }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [spec, setSpec] = useState<Spec | null>(initial);
  const [tab, setTab] = useState<Tab>(initialTab);
  const [flow, setFlow] = useState<FlowGraph | null>(null);
  const [flowState, setFlowState] = useState<"idle" | "loading" | "error">("idle");
  const [flowError, setFlowError] = useState("");
  const [sel, setSel] = useState<string>("");
  const [openApi, setOpenApi] = useState<string>("");
  const [reviewing, setReviewing] = useState(false);
  const [error, setError] = useState("");
  const req = useRef("");

  const load = useCallback((kind: "spec.load" | "spec.review" = "spec.load") => {
    req.current = `${kind}-${Date.now()}`;
    if (kind === "spec.review") setReviewing(true);
    setError("");
    postMessage(kind, { requestId: req.current });
  }, [postMessage]);
  useEffect(() => { if (!initial) load(); }, [initial, load]);
  const runDry = useCallback(() => { setFlowState("loading"); setFlowError(""); postMessage("map.flow", {}); }, [postMessage]);
  useEffect(() => { if (tab === "dry" && !flow && flowState === "idle") runDry(); }, [tab, flow, flowState, runDry]);
  useMessage(useCallback(({ type, payload }) => {
    const p = (payload ?? {}) as { requestId?: string; spec?: Spec; message?: string; review?: boolean };
    if (type === "canvas.projectChanged") { load(); setFlow(null); setFlowState("idle"); return; }
    if (type === "map.flowResult") { setFlow(payload as FlowGraph); setFlowState("idle"); return; }
    if (type === "map.flowError") { setFlowState("error"); setFlowError((payload as { message?: string })?.message || "분석하지 못했습니다."); return; }
    if (p.requestId !== req.current) return;
    if (type === "spec.result" && p.spec) { setSpec(p.spec); setReviewing(false); }
    if (type === "spec.error") { setReviewing(false); setError(p.message || "불러오지 못했습니다."); }
  }, [load]));

  const features = spec?.features ?? [];
  const current = features.find(f => f.key === sel) ?? features[0];
  const tasks = spec?.tasks ?? [];
  const openFile = useCallback((f: string, l: number) => { if (onOpenFile) onOpenFile(f, l); else postMessage("map.openFile", { id: f, line: l }); }, [onOpenFile, postMessage]);
  const dry: SpecRun | null = useMemo(() => (spec?.available && flow ? runSpec(spec, flow) : null), [spec, flow]);

  if (!spec) return <div className="rc-spec"><style>{SPEC_CSS}</style><div className="sp-empty">{error || "불러오는 중"}</div></div>;
  if (!spec.available) return <div className="rc-spec"><style>{SPEC_CSS}</style><div className="sp-empty">{spec.reason}</div></div>;
  const t = spec.tests ?? { passed: 0, total: 0, at: "", status: "" };
  const testsTone: Status = !t.total ? "unknown" : t.passed === t.total ? "ok" : "fail";
  const aiRows = current?.criteria.filter(c => c.source === "ai") ?? [];

  return (
    <div className="rc-spec" data-testid="spec-view">
      <style>{SPEC_CSS}</style>
      <div className="sp-head">
        <span className="sp-name" title={spec.project}>{spec.project}</span>
        <div className="sp-tabs" role="tablist" aria-label="명세">
          <button role="tab" aria-selected={tab === "dry"} onClick={() => setTab("dry")}>전체 흐름{dry && dry.counts.fail + dry.counts.warn > 0 && <span className="sp-count">{dry.counts.fail + dry.counts.warn}</span>}</button>
          <button role="tab" aria-selected={tab === "req"} onClick={() => setTab("req")}>요구사항</button>
          <button role="tab" aria-selected={tab === "tasks"} onClick={() => setTab("tasks")}>작업{tasks.length > 0 && <span className="sp-count">{tasks.length}</span>}</button>
        </div>
        <div className="sp-right">
          <span className="sp-checks" title={t.at ? "마지막 로컬 실행의 통합 테스트" : "아직 로컬에서 실행하지 않음"}><Ic s={testsTone} />{t.total ? `테스트 ${t.passed}/${t.total}` : "테스트 전"}</span>
          <button type="button" className="sp-btn" aria-disabled={reviewing} title={spec.review?.fresh ? spec.review.summary : "요구사항을 문장으로 정리하고 빠진 기능을 찾습니다"} onClick={() => { if (!reviewing) load("spec.review"); }}>
            {reviewing ? "검토 중" : spec.review?.fresh ? "다시 검토" : "AI 검토"}
          </button>
        </div>
      </div>
      {error && <div className="sp-err" role="alert">{error}</div>}

      {tab === "req" && <div className="sp-main">
        <nav className="sp-side" aria-label="기능">
          {features.map(f => <button key={f.key} type="button" className="sp-item" aria-current={current?.key === f.key} onClick={() => { setSel(f.key); setOpenApi(""); }}>
            <Ic s={f.status} /><span className="lb">{f.label}</span><span className="n">{f.counts.api}</span>
          </button>)}
        </nav>
        {current ? <section className="sp-doc" aria-label={current.label}>
          <h2>{current.label}</h2>
          <p className="sp-meta">{current.services.join(", ")} · API {current.counts.api}개{current.counts.criteria ? ` · 기준 ${current.counts.ok}/${current.counts.criteria} 충족` : ""}</p>
          {aiRows.length > 0 && <>
            <div className="sp-sec">수용 기준</div>
            <div className="sp-list">
              {aiRows.map((c, i) => <div className="sp-li" key={i}><Ic s={c.status} /><span className="tx">{c.text}</span>{c.status === "missing" && <span className="sp-note">구현 없음</span>}</div>)}
            </div>
          </>}
          <div className="sp-sec">API</div>
          <div className="sp-list">
            {current.endpoints.map(a => { const id = `${a.service} ${a.method} ${a.path}`; return <ApiRow key={id} a={a} criteria={current.criteria} open={openApi === id} onToggle={() => setOpenApi(openApi === id ? "" : id)} onOpenFile={openFile} />; })}
          </div>
        </section> : <div className="sp-empty">API가 없습니다.</div>}
      </div>}

      {tab === "dry" && <div>
        <div className="sp-dry-bar">
          {dry ? <>
            <span className="st"><b>API {dry.total}</b></span>
            <span className="st"><Ic s="ok" />적합 <b>{dry.counts.ok}</b></span>
            <span className="st"><Ic s="warn" />확인 필요 <b>{dry.counts.warn}</b></span>
            <span className="st"><Ic s="fail" />불일치 <b>{dry.counts.fail}</b></span>
          </> : <span>{flowState === "loading" ? "코드 전체의 호출 경로를 분석하는 중" : flowState === "error" ? flowError : "명세의 모든 API 를 코드에서 따라갑니다"}</span>}
          <span style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
            {onOpenStructure && <button type="button" className="sp-btn" onClick={onOpenStructure}>파일 구조</button>}
            <button type="button" className="sp-btn" aria-disabled={flowState === "loading"} title="코드를 실행하지 않고 호출 그래프만 따라갑니다" onClick={() => { if (flowState !== "loading") runDry(); }}>{flowState === "loading" ? "분석 중" : dry ? "다시 분석" : "분석"}</button>
          </span>
        </div>
        {dry && flow && <SystemFlow spec={spec} graph={flow} dry={dry} features={features} onOpenFile={openFile}
          statusIcon={(v, size) => <Ic s={v} size={size} />} />}
        {dry && <p className="sp-note" style={{ padding: "10px 20px 14px", margin: 0, borderTop: "1px solid var(--sp-line)" }}>코드를 실행하지 않고 호출 그래프를 따라간 결과입니다. 실제 실행 순서 · 값과 다를 수 있습니다.</p>}
      </div>}

      {tab === "tasks" && <div className="sp-pad">
        {tasks.length === 0 ? <div className="sp-empty">남은 작업이 없습니다.</div> : <div className="sp-list">
          {tasks.map(task => <div className="sp-li" key={task.id}>
            <Ic s={task.severity === "high" ? "fail" : task.severity === "medium" ? "warn" : "unknown"} />
            <span className="tx">{task.title}{task.detail && <span className="sp-sub">{task.detail}</span>}</span>
            <span className={`sp-sev ${task.severity}`}>{task.severity === "high" ? "높음" : task.severity === "medium" ? "보통" : "낮음"}</span>
            {onStartTask && task.id !== "run:local" && <button type="button" className="sp-btn" onClick={() => onStartTask(task)}>시작</button>}
          </div>)}
        </div>}
      </div>}
    </div>
  );
};

/** 작업을 코드 생성 화면에 넘길 요청 문장. */
export function taskPrompt(task: SpecTask): string {
  return `${task.title}${task.detail ? `\n${task.detail}` : ""}${task.feature ? `\n(기능: ${task.feature})` : ""}`;
}

export default SpecView;
