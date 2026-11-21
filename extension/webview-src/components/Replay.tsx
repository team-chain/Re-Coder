/** Saved deployment history, with a separate review step before rollback. */
import React, { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { useCoreReady } from "../hooks/useCoreReady";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { serviceLink } from "./EcsDeploymentProgress";
import { friendlyError } from "./ui/friendlyError";
import { Logo } from "./canvas/Logo";
import { timeAgo } from "./shell/parts";

export interface HistoryEntry {
  key: string; source: "local" | "ecs" | "stack"; deployment_id: string; project_id: string;
  target: string; region: string; status: string; status_text: string; started_at: string;
  image: string; service_url: string; error: string; remedy: string; warnings: string[];
  events: Array<{ at: string; title: string; detail: string }>;
  rollback: { available: boolean; reason: string; target: string; approval_level: number };
}
export interface HistoryData { entries: HistoryEntry[]; total: number; warnings: string[] }
export interface HistoryState {
  data: HistoryData | null; selected: string; loading: boolean; requestId: string; error: string;
  review: string; rollbackRequestId: string; busyKey: string;
  result: { key: string; message: string; ok: boolean } | null;
}
export const initialHistory: HistoryState = { data: null, selected: "", loading: false, requestId: "", error: "", review: "", rollbackRequestId: "", busyKey: "", result: null };
type Action =
  | { type: "load"; requestId: string }
  | { type: "loaded"; requestId: string; data: HistoryData }
  | { type: "error"; requestId: string; message: string }
  | { type: "select"; key: string } | { type: "review"; key: string }
  | { type: "rollback"; key: string; requestId: string }
  | { type: "result"; requestId: string; message: string; ok: boolean };
export function historyReducer(state: HistoryState, action: Action): HistoryState {
  switch (action.type) {
    case "load": return { ...state, loading: true, requestId: action.requestId, error: "", review: "" };
    case "loaded":
      if (action.requestId !== state.requestId) return state;
      return { ...state, data: action.data, loading: false, error: "", selected: action.data.entries.some(e => e.key === state.selected) ? state.selected : action.data.entries[0]?.key ?? "" };
    case "error": return action.requestId === state.requestId ? { ...state, loading: false, error: action.message } : state;
    case "select": return { ...state, selected: action.key, review: "" };
    case "review": return state.busyKey ? state : { ...state, review: action.key };
    case "rollback":
      if (state.busyKey || state.review !== action.key || !state.data?.entries.find(e => e.key === action.key)?.rollback.available) return state;
      return { ...state, busyKey: action.key, rollbackRequestId: action.requestId, review: "", result: null };
    case "result":
      if (!state.busyKey || action.requestId !== state.rollbackRequestId) return state;
      return { ...state, busyKey: "", rollbackRequestId: "", result: { key: state.busyKey, message: action.message, ok: action.ok } };
  }
}
function date(value: string): string {
  const parsed = new Date(value);
  return value && !Number.isNaN(parsed.valueOf()) ? parsed.toLocaleString("ko-KR") : "시각 미기록";
}
export function filterHistory(entries: HistoryEntry[], source: string, search: string): HistoryEntry[] {
  const term = search.trim().toLocaleLowerCase();
  return entries.filter(e => (source === "all" || e.source === source) && `${e.target} ${e.image} ${e.project_id} ${e.deployment_id}`.toLocaleLowerCase().includes(term));
}
/** 상태 → 점 색. 모르는 상태는 회색(성공으로 칠하지 않는다). */
export function historyTone(status: string): "ok" | "bad" | "warn" | "off" {
  const s = status.toLowerCase();
  if (/(success|succeeded|completed|running|healthy|deployed|ok)$/.test(s)) return "ok";
  if (/(fail|error|blocked|timed_out|unhealthy|rejected|circuit_breaker)/.test(s)) return "bad";
  if (/(rollback|rolled_back|degraded|pending|progress|warning|partial|cancel)/.test(s)) return "warn";
  return "off";
}
const SOURCE_LABEL: Record<HistoryEntry["source"], string> = { local: "로컬 Docker", stack: "로컬 다중 서비스", ecs: "AWS ECS" };
const SOURCE_LOGO: Record<HistoryEntry["source"], string> = { local: "docker", stack: "docker", ecs: "ecs" };
const CSS = `
.rc-hist{font-size:13px;line-height:1.6;color:var(--rc-fg,var(--vscode-foreground))}
.rc-hist-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.rc-hist-head h2{margin:0 auto 0 0;font-size:22px;font-weight:700;letter-spacing:-.02em}
.rc-hist-head input{height:32px;min-width:180px;flex:0 1 240px;padding:0 12px;box-sizing:border-box;border-radius:8px;border:1px solid var(--rc-line2,rgba(160,190,220,.16));background:var(--rc-tile,transparent);color:inherit;font:inherit}
.rc-hist-note{color:var(--rc-fg3,#67768A);font-size:12px;margin:8px 2px 0}
.rc-hist-list{margin-top:16px;border:1px solid var(--rc-line,rgba(160,190,220,.09));border-radius:12px;overflow:hidden;background:var(--rc-tile,transparent)}
.rc-hist-row{all:unset;box-sizing:border-box;cursor:pointer;width:100%;display:grid;grid-template-columns:150px 30px minmax(0,1fr) minmax(0,1.2fr) 90px;align-items:center;gap:14px;padding:13px 18px;border-top:1px solid var(--rc-line,rgba(160,190,220,.09));animation:rc-hist-up .35s cubic-bezier(.2,.8,.2,1) both}
.rc-hist-row:first-child{border-top:0}
.rc-hist-row:hover{background:var(--rc-tile2,rgba(148,178,210,.065))}
.rc-hist-row:focus-visible{outline:2px solid var(--rc-teal,#35A9D1);outline-offset:-2px}
.rc-hist-row[aria-expanded=true]{background:var(--rc-tile2,rgba(148,178,210,.065))}
.rc-hist-st{display:flex;align-items:center;gap:8px;font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-hist-st i{width:8px;height:8px;border-radius:50%;flex:none;background:var(--rc-fg3,#67768A)}
.rc-hist-st[data-t=ok] i{background:var(--rc-ok,#3DD68C)}.rc-hist-st[data-t=bad] i{background:var(--rc-bad,#F27272)}.rc-hist-st[data-t=warn] i{background:var(--rc-warn,#F2B544)}
.rc-hist-logo{width:28px;height:28px;border-radius:7px;border:1px solid var(--rc-line,rgba(160,190,220,.09));display:grid;place-items:center;background:var(--rc-solid,#0D131A)}
.rc-hist-logo svg{width:18px;height:18px}
.rc-hist-t{min-width:0}.rc-hist-t b{display:block;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.rc-hist-t small{color:var(--rc-fg3,#67768A);font-size:12px}
.rc-hist-img{font:12px var(--rc-mono,monospace);color:var(--rc-fg2,#9AA8B8);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-hist-when{text-align:right;color:var(--rc-fg3,#67768A);font-size:12.5px;white-space:nowrap}
.rc-hist-detail{padding:4px 18px 20px 62px;border-top:1px solid var(--rc-line,rgba(160,190,220,.09));background:var(--rc-tile2,rgba(148,178,210,.065));overflow-wrap:anywhere}
.rc-hist-detail h4{margin:14px 0 4px;font-size:14px}.rc-hist-detail .id{color:var(--rc-fg3,#67768A);font:11.5px var(--rc-mono,monospace)}
.rc-hist-detail .err{color:var(--rc-bad,#F27272);white-space:pre-wrap;margin:10px 0 4px}
.rc-hist-detail .warn{color:var(--rc-warn,#F2B544)}
.rc-hist-tl{list-style:none;margin:14px 0;padding:0 0 0 18px;border-left:2px solid var(--rc-line2,rgba(160,190,220,.16))}
.rc-hist-tl li{position:relative;margin:0 0 12px}.rc-hist-tl li:before{content:"";position:absolute;left:-24px;top:7px;width:10px;height:10px;border-radius:50%;background:var(--rc-solid,#0D131A);border:2px solid var(--rc-fg3,#67768A);box-sizing:border-box}
.rc-hist-tl li:last-child:before{border-color:var(--rc-teal,#35A9D1)}
.rc-hist-tl small{display:block;color:var(--rc-fg3,#67768A);font-size:11.5px}.rc-hist-tl div{white-space:pre-wrap;color:var(--rc-fg2,#9AA8B8)}
.rc-hist-rb{border-top:1px solid var(--rc-line,rgba(160,190,220,.09));padding-top:12px;margin-top:4px}
.rc-hist-rb code{display:block;margin:6px 0;font:12px var(--rc-mono,monospace);color:var(--rc-fg2,#9AA8B8)}
.rc-hist-confirm{margin-top:10px;padding:14px;border-radius:10px;border:1px solid color-mix(in srgb,var(--rc-warn,#F2B544) 35%,transparent);background:color-mix(in srgb,var(--rc-warn,#F2B544) 7%,transparent)}
.rc-hist-confirm p{margin:6px 0}.rc-hist-acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.rc-hist-empty{padding:40px 20px;text-align:center;color:var(--rc-fg3,#67768A)}
.rc-hist-skel{height:54px;border-top:1px solid var(--rc-line,rgba(160,190,220,.09));background:linear-gradient(90deg,transparent,var(--rc-tile2,rgba(148,178,210,.065)),transparent);background-size:200% 100%;animation:rc-hist-sh 1.3s linear infinite}
@keyframes rc-hist-up{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes rc-hist-sh{to{background-position:-200% 0}}
@media(prefers-reduced-motion:reduce){.rc-hist *{animation:none!important}}
@media(max-width:720px){.rc-hist-row{grid-template-columns:24px minmax(0,1fr) auto;gap:10px;padding:12px 14px}.rc-hist-row .rc-hist-st{grid-column:1/-1;order:-1}.rc-hist-img{display:none}.rc-hist-detail{padding-left:14px}}
`;
export function ReplayView({ state, source, search, onSource, onSearch, onRefresh, onSelect, onReview, onConfirm }: {
  state: HistoryState; source: string; search: string;
  onSource: (source: string) => void; onSearch: (search: string) => void; onRefresh: () => void;
  onSelect: (key: string) => void; onReview: (key: string) => void; onConfirm: (entry: HistoryEntry) => void;
}) {
  const rows = filterHistory(state.data?.entries ?? [], source, search);
  const selected = rows.find(e => e.key === state.selected);
  const busy = Boolean(state.busyKey);
  return <section className="rc-hist" aria-label="배포 이력 및 롤백">
    <style>{CSS}</style>
    <div className="rc-hist-head rc-replay-bar">
      <h2>배포 기록</h2>
      <div className="rc-seg" role="group" aria-label="배포 유형">
        {([["all", "전체"], ["local", "로컬"], ["stack", "다중 서비스"], ["ecs", "ECS"]] as const).map(([value, label]) => <button key={value} type="button" aria-pressed={source === value} onClick={() => onSource(value)}>{label}</button>)}
      </div>
      <input aria-label="배포 이력 검색" value={search} placeholder="서비스 · 이미지 검색" onChange={e => onSearch(e.target.value)} />
      <button type="button" className="rc-btn sm" onClick={onRefresh} disabled={state.loading || busy} aria-disabled={state.loading || busy}>{state.loading ? "불러오는 중…" : "새로고침"}</button>
    </div>
    {state.error && <p role="alert" className="rc-hist-note" style={{ color: "var(--rc-bad,#F27272)", whiteSpace: "pre-wrap" }}>이력 조회 실패: {friendlyError(state.error)}{state.data && " · 마지막 조회 결과를 유지합니다."}</p>}
    {!!state.data?.warnings.length && <div role="alert" className="rc-hist-note" style={{ color: "var(--rc-warn,#F2B544)" }}>{state.data.warnings.map((warning, i) => <p key={i} style={{ margin: 0 }}>{warning}</p>)}</div>}
    {state.data && state.data.total > state.data.entries.length && <p className="rc-hist-note">저장된 기록 {state.data.total}건 중 최근 {state.data.entries.length}건</p>}
    <div className="rc-hist-list" role="list">
      {!state.data && !state.error && <><p role="status" className="rc-hist-note" style={{ margin: "12px 18px" }}>배포 이력을 불러오는 중…</p>{[0, 1, 2].map(i => <div key={i} className="rc-hist-skel" />)}</>}
      {!state.data && state.error && <div className="rc-hist-empty"><button type="button" className="rc-btn" onClick={onRefresh} disabled={state.loading}>{state.loading ? "불러오는 중…" : "다시 시도"}</button></div>}
      {state.data && rows.length === 0 && <div className="rc-hist-empty">{state.data.total === 0 ? "저장된 배포 기록이 없습니다. 배포하면 여기에 쌓여요." : "조건에 맞는 배포 기록이 없습니다."}</div>}
      {rows.map((row, i) => {
        const open = row.key === state.selected;
        return <div key={row.key} role="listitem">
          <button type="button" className="rc-hist-row" aria-expanded={open} aria-pressed={open} onClick={() => onSelect(open ? "" : row.key)} style={{ animationDelay: `${Math.min(i, 10) * 0.03}s` }}>
            <span className="rc-hist-st" data-t={historyTone(row.status)}><i />{row.status_text}</span>
            <span className="rc-hist-logo" aria-hidden><Logo kind={SOURCE_LOGO[row.source]} /></span>
            <span className="rc-hist-t"><b>{row.target}</b><small>{SOURCE_LABEL[row.source]}{row.region && ` · ${row.region}`}</small></span>
            <span className="rc-hist-img" title={row.image}>{row.image}</span>
            <span className="rc-hist-when" title={date(row.started_at)}>{timeAgo(row.started_at) || date(row.started_at)}</span>
          </button>
          {open && selected && <HistoryDetail entry={selected} state={state} busy={busy} onReview={onReview} onConfirm={onConfirm} />}
        </div>;
      })}
    </div>
    <p className="rc-hist-note">이 컴퓨터에 저장된 배포 기록입니다. 현재 실행 상태와 과금 여부는 별도로 확인하세요.</p>
  </section>;
}
/** 모든 항목이 통과한 기록인지 — "배포 검사 45/45 통과". 실패·경고 단어가 섞이면 경고로 둔다. */
export function isPassNote(text: string): boolean {
  const m = /(\d+)\s*\/\s*(\d+)\s*통과/.exec(text);
  return !!m && m[1] === m[2] && !/실패|경고|확인 필요|건너/.test(text);
}
export function isIsoTime(text: string): boolean { return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(text) && !Number.isNaN(Date.parse(text)); }
function HistoryDetail({ entry: selected, state, busy, onReview, onConfirm }: { entry: HistoryEntry; state: HistoryState; busy: boolean; onReview: (key: string) => void; onConfirm: (entry: HistoryEntry) => void }) {
  const link = serviceLink(selected.service_url);
  const result = state.result?.key === selected.key ? state.result : null;
  return <article className="rc-hist-detail">
    <h4>{selected.target} · {selected.status_text}</h4><div className="id">배포 ID {selected.deployment_id} · {date(selected.started_at)}</div>
    {selected.error && <p className="err">{selected.error}</p>}{selected.remedy && <p>조치: {selected.remedy}</p>}
    {link && <a href={link} target="_blank" rel="noreferrer">기록된 서비스 URL 열기 ↗</a>}
    {(() => {
      //: "배포 검사 45/45 통과" 같은 통과 기록은 경고색으로 칠하지 않는다(실사용 2026-11-20).
      const passed = selected.warnings.filter(isPassNote), warns = selected.warnings.filter(w => !isPassNote(w));
      return <>
        {!!passed.length && <ul className="rc-hist-ok" style={{ paddingLeft: 20, margin: "12px 0 4px", color: "var(--rc-ok,#3DD68C)" }}>{passed.map((w, i) => <li key={i}>{w}</li>)}</ul>}
        {!!warns.length && <div className="warn" style={{ marginTop: 12 }}><b>기록 당시의 안내</b><ul style={{ paddingLeft: 20, margin: "4px 0" }}>{warns.map((warning, i) => <li key={i}>{warning}</li>)}</ul></div>}
      </>;
    })()}
    {!!selected.events.length && <ol className="rc-hist-tl">{selected.events.map((event, i) => <li key={i}><b>{event.title}</b><small>{date(event.at)}</small>{event.detail && <div>{event.detail}</div>}</li>)}</ol>}
    <div className="rc-hist-rb"><b>이전 버전으로 복귀</b>
      {selected.rollback.target && (isIsoTime(selected.rollback.target) ? <p style={{ margin: "4px 0 0" }}>{date(selected.rollback.target)} 에 배포한 버전으로</p> : <code>{selected.rollback.target}</code>)}
      {!selected.rollback.available && <p style={{ color: "var(--rc-fg3,#67768A)", margin: "4px 0 0" }}>{selected.rollback.reason}</p>}
      {selected.rollback.available && state.review !== selected.key && <div className="rc-hist-acts"><button type="button" className="rc-btn" onClick={() => onReview(selected.key)} disabled={busy || state.loading}>{state.busyKey === selected.key ? "롤백 처리 중…" : "롤백 대상 확인"}</button></div>}
      {state.review === selected.key && <div role="group" aria-label="롤백 실행 확인" className="rc-hist-confirm"><b>{selected.source === "ecs" ? "ECS 롤백 승인 · Level 3" : selected.source === "stack" ? "다중 서비스 롤백 승인" : "로컬 롤백 승인"}</b><p><b>{selected.target}</b>를 위의 이전 버전으로 되돌립니다. 실행 중인 서비스에 영향을 줍니다.</p>{selected.source === "ecs" && <p>요청 후 ECS 서비스 안정화 상태를 확인해야 합니다.</p>}{selected.source === "stack" && <p>모든 서비스를 직전 성공 버전 이미지로 되돌립니다. DB 데이터(볼륨)는 그대로입니다.</p>}<div className="rc-hist-acts"><button type="button" className="rc-btn p" disabled={busy || state.loading} onClick={() => onConfirm(selected)}>승인하고 롤백</button><button type="button" className="rc-btn" disabled={busy} onClick={() => onReview("")}>취소</button></div></div>}
      {result && <p role="status" style={{ whiteSpace: "pre-wrap", color: result.ok ? "var(--rc-ok,#3DD68C)" : "var(--rc-bad,#F27272)" }}>{result.message}</p>}
    </div>
  </article>;
}
export const Replay: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [state, dispatch] = useReducer(historyReducer, initialHistory);
  const [source, setSource] = useState("all");
  const [search, setSearch] = useState("");
  const sequence = useRef(0);
  const instance = useRef(`history-${Date.now()}-${Math.random()}`);
  const pendingRollback = useRef("");
  const load = useCallback(() => {
    const requestId = `${instance.current}-${++sequence.current}`;
    dispatch({ type: "load", requestId }); postMessage("replay.history", { requestId });
  }, [postMessage]);
  useCoreReady(() => load());
  useEffect(() => { load(); }, [load]);
  useMessage(useCallback(({ type, payload }) => {
    const p = payload as { requestId: string; history: HistoryData; message?: string; status?: string; warning?: string; auditWarning?: string };
    if (type === "replay.historyResult") dispatch({ type: "loaded", requestId: p.requestId, data: p.history });
    if (type === "replay.historyError") dispatch({ type: "error", requestId: p.requestId, message: p.message ?? "조회에 실패했습니다." });
    if ((type === "replay.rollbackResult" || type === "replay.rollbackError") && pendingRollback.current === p.requestId) {
      pendingRollback.current = "";
      dispatch({ type: "result", requestId: p.requestId, message: [p.message, p.warning, p.auditWarning].filter(Boolean).join("\n"), ok: type === "replay.rollbackResult" && ["ok", "completed"].includes(p.status ?? "") }); load();
    }
  }, [load]));
  const confirm = (entry: HistoryEntry) => {
    if (pendingRollback.current || state.loading || state.review !== entry.key || !entry.rollback.available) return;
    const requestId = `${instance.current}-rollback-${++sequence.current}`; pendingRollback.current = requestId;
    dispatch({ type: "rollback", key: entry.key, requestId }); postMessage("replay.rollback", { source: entry.source, deploymentId: entry.deployment_id, approved: true, requestId });
  };
  return <ReplayView state={state} source={source} search={search} onSource={setSource} onSearch={setSearch} onRefresh={load} onSelect={key => dispatch({ type: "select", key })} onReview={key => dispatch({ type: "review", key })} onConfirm={confirm} />;
};
export default Replay;
