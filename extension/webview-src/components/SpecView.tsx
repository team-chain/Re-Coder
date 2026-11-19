/**
 * 명세 — 요구사항 · 설계 · 작업 (Kiro 의 명세 화면을 참고, 4.0.1)
 *
 * 실사용 2026-11-19: 아키텍처 화면이 API 하나씩(dry run) · 파일 그래프만 보여 "전체가 요청에 맞게 만들어졌는지" 알 수 없었다.
 * 한 프로젝트를 기능 단위로 본다. 글은 최소로 — 상태는 점 · 체크 표시, 설명은 누를 때만.
 *   · 요구사항: 왼쪽 기능 목록, 오른쪽 수용 기준(WHEN … 한다)과 그 기능의 API
 *   · 설계: 서비스 · DB 구성과 전체 API 표
 *   · 작업: 고칠 것. "시작" 을 누르면 코드 생성 화면으로 넘긴다
 * 데이터는 Core /api/project/spec (계약 · 코드 · 마지막 통합 테스트 · 저장된 AI 검토). AI 검토는 "검토" 를 누를 때만.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

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
export interface SpecService { name: string; kind: string; language: string; port: number; public: boolean; databases: string[]; depends_on: string[]; purpose: string; api: number }
export interface Spec {
  available: boolean; reason?: string; project?: string;
  features?: SpecFeature[]; services?: SpecService[]; databases?: { name: string; engine: string; tables: string[] }[];
  endpoints?: SpecApi[]; tasks?: SpecTask[];
  tests?: { passed: number; total: number; at: string; status: string };
  review?: { at: string; summary: string; fresh: boolean; missing: string[] } | null;
}

export const SPEC_CSS = `
.rc-spec{--sp-line:var(--rc-line2,rgba(160,190,220,.14));--sp-soft:var(--rc-tile,rgba(148,178,210,.035));--sp-soft2:var(--rc-tile2,rgba(148,178,210,.065));
  border:1px solid var(--sp-line);border-radius:14px;overflow:hidden;background:var(--rc-solid,#0d131a);color:var(--rc-fg,#eef3f8);font-size:13px}
.rc-spec button{font:inherit;color:inherit}
.sp-bar{display:flex;align-items:center;gap:12px;height:46px;padding:0 14px;border-bottom:1px solid var(--sp-line)}
.sp-bar .nm{font-weight:650}.sp-bar .nm small{font-weight:500;color:var(--rc-fg3,#67768a);margin-left:8px}
.sp-seg{display:flex;gap:2px;padding:3px;border-radius:9px;background:var(--sp-soft2);margin:0 auto}
.sp-seg button{all:unset;cursor:pointer;padding:4px 13px;border-radius:7px;font-size:12.5px;color:var(--rc-fg3,#67768a)}
.sp-seg button[aria-selected=true]{background:var(--rc-solid,#0d131a);color:var(--rc-fg,#eef3f8);box-shadow:0 0 0 1px var(--sp-line)}
.sp-seg em{font-style:normal;margin-left:5px;font-size:11px;padding:0 6px;border-radius:999px;background:var(--sp-soft2);color:var(--rc-fg2,#9aa8b8)}
.sp-tests{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--rc-fg2,#9aa8b8);white-space:nowrap}
.sp-btn{all:unset;cursor:pointer;font-size:12px;font-weight:600;padding:6px 12px;border-radius:8px;border:1px solid var(--sp-line);white-space:nowrap}
.sp-btn:hover{background:var(--sp-soft2)}.sp-btn[aria-disabled=true]{opacity:.5;cursor:default}
.sp-btn.pri{background:var(--rc-fg,#eef3f8);color:var(--rc-solid,#0d131a);border-color:transparent}
.sp-dot{width:7px;height:7px;border-radius:50%;flex:none;display:inline-block;background:var(--rc-fg3,#67768a);opacity:.55}
.sp-dot.ok{background:var(--rc-ok,#3dd68c);opacity:1}.sp-dot.fail{background:var(--rc-bad,#f27272);opacity:1}
.sp-dot.warn,.sp-dot.missing,.sp-dot.partial{background:var(--rc-warn,#f2b544);opacity:1}
.sp-main{display:grid;grid-template-columns:230px minmax(0,1fr);min-height:420px}
.sp-side{border-right:1px solid var(--sp-line);padding:10px 8px}
.sp-side h4{margin:0;padding:6px 10px 8px;font-size:11px;font-weight:600;color:var(--rc-fg3,#67768a);letter-spacing:.04em}
.sp-item{all:unset;box-sizing:border-box;cursor:pointer;width:100%;display:flex;align-items:center;gap:10px;padding:7px 10px;border-radius:8px;color:var(--rc-fg2,#9aa8b8)}
.sp-item:hover{background:var(--sp-soft)}.sp-item[aria-current=true]{background:var(--sp-soft2);color:var(--rc-fg,#eef3f8)}
.sp-item small{margin-left:auto;color:var(--rc-fg3,#67768a);font-variant-numeric:tabular-nums}
.sp-doc{padding:20px 26px;min-width:0}
.sp-doc h2{margin:0;font-size:17px;font-weight:650}
.sp-meta{display:flex;flex-wrap:wrap;gap:4px 14px;margin:4px 0 16px;font-size:12px;color:var(--rc-fg3,#67768a)}
.sp-box{border:1px solid var(--sp-line);border-radius:10px;overflow:hidden}
.sp-box+.sp-box{margin-top:14px}
.sp-row{display:flex;align-items:center;gap:12px;padding:9px 14px;border-top:1px solid var(--sp-line)}
.sp-row:first-child{border-top:0}
.sp-chk{width:16px;height:16px;border-radius:50%;flex:none;display:grid;place-items:center;background:var(--sp-soft2);color:var(--rc-fg3,#67768a)}
.sp-chk.ok{background:color-mix(in srgb,var(--rc-ok,#3dd68c) 18%,transparent);color:var(--rc-ok,#3dd68c)}
.sp-chk.fail{background:color-mix(in srgb,var(--rc-bad,#f27272) 18%,transparent);color:var(--rc-bad,#f27272)}
.sp-chk.missing{background:color-mix(in srgb,var(--rc-warn,#f2b544) 18%,transparent);color:var(--rc-warn,#f2b544)}
.sp-cond{width:42px;flex:none;font-size:10.5px;font-weight:700;color:var(--rc-fg3,#67768a)}
.sp-txt{flex:1;min-width:0}.sp-txt small{color:var(--rc-fg3,#67768a);margin-left:6px}
.sp-api{all:unset;box-sizing:border-box;cursor:pointer;display:grid;grid-template-columns:58px minmax(0,1fr) auto 16px;gap:10px;align-items:center;width:100%;padding:8px 14px;border-top:1px solid var(--sp-line)}
.sp-api:first-child{border-top:0}.sp-api:hover{background:var(--sp-soft)}
.sp-m{font:600 10.5px var(--rc-mono,monospace);color:var(--rc-fg3,#67768a)}
.sp-p{font:12px var(--rc-mono,monospace);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sp-au{font-size:11px;color:var(--rc-fg3,#67768a)}
.sp-more{padding:8px 14px 12px 82px;font-size:12px;color:var(--rc-fg2,#9aa8b8);display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;border-top:1px dashed var(--sp-line)}
.sp-link{all:unset;cursor:pointer;color:var(--rc-teal,#35a9d1)}.sp-link:hover{text-decoration:underline}
.sp-hint{padding:10px 14px;font-size:12px;color:var(--rc-fg3,#67768a);display:flex;align-items:center;gap:10px}
.sp-pad{padding:18px}
.sp-svcs{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.sp-svc{border:1px solid var(--sp-line);border-radius:10px;padding:9px 11px}
.sp-svc b{font-weight:600}.sp-svc small{display:block;color:var(--rc-fg3,#67768a);font-size:11.5px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sp-svc .k{float:right;font-size:10.5px;color:var(--rc-fg3,#67768a)}
.sp-h{font-size:12px;font-weight:600;color:var(--rc-fg2,#9aa8b8);margin:18px 0 8px}.sp-h:first-child{margin-top:0}
.sp-task{display:flex;align-items:center;gap:12px;padding:11px 14px;border-top:1px solid var(--sp-line)}.sp-task:first-child{border-top:0}
.sp-sq{width:15px;height:15px;border:1.5px solid var(--rc-fg3,#67768a);border-radius:4px;flex:none;opacity:.7}
.sp-task .sp-txt small{display:block;margin:2px 0 0}
.sp-sev{font-size:10.5px;font-weight:700;padding:1px 7px;border-radius:999px;flex:none}
.sp-sev.high{color:var(--rc-bad,#f27272);background:color-mix(in srgb,var(--rc-bad,#f27272) 14%,transparent)}
.sp-sev.medium{color:var(--rc-warn,#f2b544);background:color-mix(in srgb,var(--rc-warn,#f2b544) 14%,transparent)}
.sp-sev.low{color:var(--rc-fg3,#67768a);background:var(--sp-soft2)}
.sp-empty{padding:40px 20px;text-align:center;color:var(--rc-fg3,#67768a)}
.sp-sum{margin:0 0 14px;padding:10px 14px;border-radius:10px;background:var(--sp-soft2);font-size:12.5px;display:flex;gap:10px;align-items:baseline}
.sp-sum small{color:var(--rc-fg3,#67768a);margin-left:auto;white-space:nowrap}
.sp-err{margin:10px 14px 0;padding:9px 12px;border-radius:9px;font-size:12px;background:color-mix(in srgb,var(--rc-bad,#f27272) 12%,transparent);color:var(--rc-bad,#f27272)}
@media(max-width:760px){.sp-main{grid-template-columns:1fr}.sp-side{border-right:0;border-bottom:1px solid var(--sp-line);display:flex;overflow-x:auto;gap:4px}.sp-side h4{display:none}.sp-item{width:auto;white-space:nowrap}.sp-seg{margin:0}.sp-bar{flex-wrap:wrap;height:auto;padding:8px 12px}}
`;

const Check: React.FC<{ s: Status }> = ({ s }) => (
  <span className={`sp-chk ${s === "ok" ? "ok" : s === "fail" ? "fail" : s === "missing" ? "missing" : ""}`} aria-label={STATUS_LABEL[s]}>
    {s === "ok" ? <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
      : s === "fail" ? <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg>
        : s === "missing" ? <b style={{ fontSize: 10 }}>!</b> : <span style={{ width: 5, height: 5, borderRadius: "50%", background: "currentColor", opacity: .6 }} />}
  </span>
);
export const STATUS_LABEL: Record<Status, string> = { ok: "통과", fail: "실패", missing: "없음", warn: "확인 필요", partial: "일부 확인", unknown: "아직 확인 안 함" };

/** 테스트에서 나온 기준을 짧게 — 같은 문장이 반복되지 않게 API 를 앞세운다. */
const testBrief = (c: SpecCriterion): string =>
  c.text.startsWith("로그인하지") ? "로그인 없이 부르면 거절" : c.text.startsWith("요청을 받으면") ? "서버 오류 없이 응답"
    : c.text.replace(/^올바른 요청에 /, "").replace(/ 로 응답한다$/, " 응답").replace(/한다$/, "");

function authLabel(a: SpecApi): string { return a.admin ? "관리자" : a.auth ? "로그인" : "공개"; }
function apiStatus(a: SpecApi): Status {
  if (!a.implemented) return "fail";
  if (a.tests.some(t => t.status === "fail")) return "fail";
  if (a.tests.length && a.tests.every(t => t.status === "ok")) return "ok";
  return "unknown";
}
function ago(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso); if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

const ApiRow: React.FC<{ a: SpecApi; criteria: SpecCriterion[]; open: boolean; onToggle: () => void; onOpenFile: (f: string, l: number) => void }> = ({ a, criteria, open, onToggle, onOpenFile }) => {
  const mine = criteria.filter(c => c.source === "test" && c.api === `${a.method} ${a.path}`);
  return <>
    <button type="button" className="sp-api" aria-expanded={open} onClick={onToggle}>
      <span className="sp-m">{a.method}</span><span className="sp-p" title={a.path}>{a.path}</span>
      <span className="sp-au">{authLabel(a)}</span><span className={`sp-dot ${apiStatus(a)}`} aria-label={STATUS_LABEL[apiStatus(a)]} />
    </button>
    {open && <div className="sp-more">
      {a.implemented
        ? <button type="button" className="sp-link" onClick={() => onOpenFile(a.file, a.line)}>{a.file.split("/").slice(-2).join("/")}:{a.line}</button>
        : <span style={{ color: "var(--rc-bad,#f27272)" }}>코드에서 찾지 못함</span>}
      <span>{a.service}</span>
      {mine.map(c => <span key={c.test} style={{ display: "inline-flex", gap: 6, alignItems: "center" }}><Check s={c.status} />{testBrief(c)}{c.got !== undefined && c.status === "fail" ? ` · 실제 ${c.got}` : ""}</span>)}
      {!mine.length && <span>테스트 없음</span>}
    </div>}
  </>;
};

export const SpecView: React.FC<{ initial?: Spec | null; onStartTask?: (task: SpecTask) => void; onOpenFile?: (file: string, line: number) => void; onOpenStructure?: () => void }> = ({ initial = null, onStartTask, onOpenFile, onOpenStructure }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [spec, setSpec] = useState<Spec | null>(initial);
  const [tab, setTab] = useState<"req" | "design" | "tasks">("req");
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
  useMessage(useCallback(({ type, payload }) => {
    const p = (payload ?? {}) as { requestId?: string; spec?: Spec; message?: string; review?: boolean };
    if (type === "canvas.projectChanged") { load(); return; }
    if (p.requestId !== req.current) return;
    if (type === "spec.result" && p.spec) { setSpec(p.spec); setReviewing(false); }
    if (type === "spec.error") { setReviewing(false); setError(p.review ? `검토 실패 — ${p.message}` : p.message || "명세를 불러오지 못했어요."); }
  }, [load]));

  const features = spec?.features ?? [];
  const current = features.find(f => f.key === sel) ?? features[0];
  const tasks = spec?.tasks ?? [];
  const openFile = useCallback((f: string, l: number) => { if (onOpenFile) onOpenFile(f, l); else postMessage("map.openFile", { id: f, line: l }); }, [onOpenFile, postMessage]);
  const startTask = useCallback((t: SpecTask) => onStartTask?.(t), [onStartTask]);
  const groupedApis = useMemo(() => {
    const out: Record<string, SpecApi[]> = {};
    for (const a of spec?.endpoints ?? []) (out[a.service] ??= []).push(a);
    return out;
  }, [spec]);

  if (!spec) return <div className="rc-spec"><style>{SPEC_CSS}</style><div className="sp-empty">{error || "명세 불러오는 중…"}</div></div>;
  if (!spec.available) return <div className="rc-spec"><style>{SPEC_CSS}</style><div className="sp-empty">{spec.reason}</div></div>;
  const t = spec.tests ?? { passed: 0, total: 0, at: "", status: "" };
  const testsTone: Status = t.total === 0 ? "unknown" : t.passed === t.total ? "ok" : "fail";

  return (
    <div className="rc-spec" data-testid="spec-view">
      <style>{SPEC_CSS}</style>
      <div className="sp-bar">
        <span className="nm">{spec.project}<small>명세</small></span>
        <div className="sp-seg" role="tablist" aria-label="명세">
          <button role="tab" aria-selected={tab === "req"} onClick={() => setTab("req")}>요구사항</button>
          <button role="tab" aria-selected={tab === "design"} onClick={() => setTab("design")}>설계</button>
          <button role="tab" aria-selected={tab === "tasks"} onClick={() => setTab("tasks")}>작업{tasks.length > 0 && <em>{tasks.length}</em>}</button>
        </div>
        <span className="sp-tests" title={t.at ? `마지막 로컬 실행 ${ago(t.at)}` : "아직 로컬 실행을 하지 않았어요"}>
          <span className={`sp-dot ${testsTone}`} />{t.total ? `테스트 ${t.passed}/${t.total}` : "테스트 전"}
        </span>
        <button type="button" className="sp-btn" aria-disabled={reviewing} onClick={() => { if (!reviewing) load("spec.review"); }}
          title="AI 가 요구사항을 정리하고 빠진 기능을 찾습니다(모델 호출 1회)">{reviewing ? "검토 중…" : spec.review?.fresh ? "다시 검토" : "검토"}</button>
      </div>
      {error && <div className="sp-err" role="alert">{error}</div>}

      {tab === "req" && <div className="sp-main">
        <nav className="sp-side" aria-label="기능">
          <h4>기능 {features.filter(f => !f.internal).length}</h4>
          {features.map(f => <button key={f.key} type="button" className="sp-item" aria-current={current?.key === f.key} onClick={() => { setSel(f.key); setOpenApi(""); }}>
            <span className={`sp-dot ${f.status}`} />{f.label}<small>{f.counts.api}</small>
          </button>)}
        </nav>
        {current ? <section className="sp-doc" aria-label={current.label}>
          <h2>{current.label}</h2>
          <div className="sp-meta"><span>{current.services.join(" · ")}</span><span>API {current.counts.api}</span><span>{STATUS_LABEL[current.status]}</span></div>
          {spec.review?.fresh && spec.review.summary && current === features[0] && <p className="sp-sum">{spec.review.summary}<small>{ago(spec.review.at)}</small></p>}
          {(current.criteria.some(c => c.source === "ai") || !spec.review?.fresh) && <div className="sp-box">
            {current.criteria.filter(c => c.source === "ai").map((c, i) => <div className="sp-row" key={`ai${i}`}>
              <Check s={c.status} /><span className="sp-cond">{c.cond}</span><span className="sp-txt">{c.text}{c.status === "missing" && <small>API 없음</small>}</span>
            </div>)}
            {!current.criteria.some(c => c.source === "ai") && !spec.review?.fresh && <div className="sp-hint">
              {spec.review && !spec.review.fresh ? "설계가 바뀌었어요. 다시 검토하면 요구사항을 새로 정리해요." : "검토를 누르면 요구사항을 문장으로 정리하고 빠진 기능을 찾아요."}
            </div>}
          </div>}
          <div className="sp-box">
            {current.endpoints.map(a => { const id = `${a.service} ${a.method} ${a.path}`; return <ApiRow key={id} a={a} criteria={current.criteria} open={openApi === id} onToggle={() => setOpenApi(openApi === id ? "" : id)} onOpenFile={openFile} />; })}
          </div>
        </section> : <div className="sp-empty">API 가 없어요.</div>}
      </div>}

      {tab === "design" && <div className="sp-pad">
        <div className="sp-h">서비스 {spec.services?.length ?? 0} · DB {spec.databases?.length ?? 0}</div>
        <div className="sp-svcs">
          {(spec.services ?? []).map(s => <div className="sp-svc" key={s.name}>
            <span className="k">{s.kind === "web" ? "화면" : s.kind === "worker" ? "워커" : `API ${s.api}`}</span><b>{s.name}</b>
            <small>{[s.port ? `:${s.port}` : "", s.databases.join(" · "), s.depends_on.length ? `→ ${s.depends_on.join(", ")}` : ""].filter(Boolean).join("  ") || " "}</small>
          </div>)}
          {(spec.databases ?? []).map(d => <div className="sp-svc" key={d.name}><span className="k">{d.engine}</span><b>{d.name}</b><small>{d.tables.join(" · ") || " "}</small></div>)}
        </div>
        {Object.entries(groupedApis).map(([svc, apis]) => <React.Fragment key={svc}>
          <div className="sp-h">{svc} <span style={{ fontWeight: 400, color: "var(--rc-fg3,#67768a)" }}>{apis.length}</span></div>
          <div className="sp-box">{apis.map(raw => {
            const id = `${raw.service} ${raw.method} ${raw.path}`;
            //: 기능 쪽 API 가 테스트 결과를 들고 있다 — 같은 API 를 찾아 그 결과로 그린다.
            const same = (x: SpecApi) => x.service === raw.service && x.method === raw.method && x.path === raw.path;
            const f = features.find(x => x.endpoints.some(same));
            const a = f?.endpoints.find(same) ?? raw;
            return <ApiRow key={id} a={a} criteria={f?.criteria ?? []} open={openApi === id} onToggle={() => setOpenApi(openApi === id ? "" : id)} onOpenFile={openFile} />;
          })}</div>
        </React.Fragment>)}
        {onOpenStructure && <p style={{ marginTop: 16 }}><button type="button" className="sp-link" onClick={onOpenStructure}>파일 구조 · 호출 흐름 보기 ›</button></p>}
      </div>}

      {tab === "tasks" && <div className="sp-pad">
        {tasks.length === 0 ? <div className="sp-empty">할 작업이 없어요.{!spec.review?.fresh && " 검토를 누르면 빠진 기능을 찾아요."}</div> : <div className="sp-box">
          {tasks.map(task => <div className="sp-task" key={task.id}>
            <span className="sp-sq" aria-hidden />
            <span className="sp-txt">{task.title}{task.detail && <small>{task.detail}</small>}</span>
            <span className={`sp-sev ${task.severity}`}>{task.severity === "high" ? "높음" : task.severity === "medium" ? "보통" : "낮음"}</span>
            {onStartTask && task.id !== "run:local" && <button type="button" className="sp-btn" onClick={() => startTask(task)}>시작</button>}
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
