/** Saved deployment history, with a separate review step before rollback. */
import React, { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import { serviceLink } from "./EcsDeploymentProgress";

export interface HistoryEntry {
  key: string; source: "local" | "ecs"; deployment_id: string; project_id: string;
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
const muted = "var(--vscode-descriptionForeground, #aaa)";
const border = "1px solid var(--vscode-panel-border, #444)";
const card: React.CSSProperties = { border, borderRadius: 7, padding: 14, marginTop: 12, overflowWrap: "anywhere" };
const button: React.CSSProperties = { border, borderRadius: 5, padding: "7px 12px", cursor: "pointer", color: "var(--vscode-button-foreground, white)", background: "var(--vscode-button-background, #0078d4)" };
function date(value: string): string {
  const parsed = new Date(value);
  return value && !Number.isNaN(parsed.valueOf()) ? parsed.toLocaleString("ko-KR") : "시각 미기록";
}
export function filterHistory(entries: HistoryEntry[], source: string, search: string): HistoryEntry[] {
  const term = search.trim().toLocaleLowerCase();
  return entries.filter(e => (source === "all" || e.source === source) && `${e.target} ${e.image} ${e.project_id} ${e.deployment_id}`.toLocaleLowerCase().includes(term));
}
export function ReplayView({ state, source, search, onSource, onSearch, onRefresh, onSelect, onReview, onConfirm }: {
  state: HistoryState; source: string; search: string;
  onSource: (source: string) => void; onSearch: (search: string) => void; onRefresh: () => void;
  onSelect: (key: string) => void; onReview: (key: string) => void; onConfirm: (entry: HistoryEntry) => void;
}) {
  const rows = filterHistory(state.data?.entries ?? [], source, search);
  const selected = rows.find(e => e.key === state.selected);
  const link = serviceLink(selected?.service_url);
  const busy = Boolean(state.busyKey);
  const result = selected && state.result?.key === selected.key ? state.result : null;
  return <section style={{ fontSize: 13, lineHeight: 1.6 }} aria-label="배포 이력 및 롤백">
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center" }}><h3 style={{ margin: 0 }}>배포 이력 · 롤백</h3><button onClick={onRefresh} disabled={state.loading || busy} style={button}>{state.loading ? "불러오는 중…" : "새로고침"}</button></div>
    <p style={{ color: muted }}>이 컴퓨터에 저장된 배포 기록입니다. 현재 실행 상태와 과금 여부는 별도로 확인하세요.</p>
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      <select aria-label="배포 유형" value={source} onChange={e => onSource(e.target.value)} style={{ padding: 7 }}><option value="all">전체</option><option value="ecs">ECS</option><option value="local">로컬</option></select>
      <input aria-label="배포 이력 검색" value={search} placeholder="서비스·컨테이너·이미지 검색" onChange={e => onSearch(e.target.value)} style={{ flex: 1, minWidth: 150, padding: 7 }} />
    </div>
    {state.error && <p role="alert" style={{ color: "var(--vscode-errorForeground, #f48771)", whiteSpace: "pre-wrap" }}>이력 조회 실패: {state.error}{state.data && " · 마지막 조회 결과를 유지합니다."}</p>}
    {!!state.data?.warnings.length && <div role="alert" style={{ color: "var(--vscode-editorWarning-foreground, #cca700)" }}>{state.data.warnings.map((warning, i) => <p key={i}>{warning}</p>)}</div>}
    {!state.data && !state.error && <p role="status">배포 이력을 불러오는 중…</p>}
    {state.data && <p style={{ color: muted }}>{rows.length}건 표시 · 저장된 기록 {state.data.total}건{state.data.total > state.data.entries.length && ` 중 최근 ${state.data.entries.length}건을 조회했습니다.`}</p>}
    {state.data && rows.length === 0 && <p>{state.data.total === 0 ? "저장된 배포 기록이 없습니다. 배포 후 이곳에 이력이 표시됩니다." : "조건에 맞는 배포 기록이 없습니다."}</p>}
    <div style={{ display: "grid", gap: 6, maxHeight: 330, overflowY: "auto", marginTop: 8 }}>
      {rows.map(row => <button key={row.key} aria-pressed={row.key === state.selected} onClick={() => onSelect(row.key)} style={{ textAlign: "left", padding: "10px 12px", border: row.key === state.selected ? "1px solid var(--vscode-focusBorder, #007fd4)" : border, borderRadius: 6, background: row.key === state.selected ? "var(--vscode-list-activeSelectionBackground, #23435b)" : "var(--vscode-editorWidget-background, #252526)", color: "var(--vscode-editor-foreground, #ddd)", cursor: "pointer", overflowWrap: "anywhere" }}>
        <b>{row.source === "ecs" ? "ECS" : "로컬"} · {row.target}</b> <span style={{ marginLeft: 8 }}>{row.status_text}</span><div style={{ fontSize: 11 }}>{date(row.started_at)}{row.region && ` · ${row.region}`}</div><div style={{ fontSize: 11, color: muted }}>{row.image}</div>
      </button>)}
    </div>
    {rows.length > 0 && !selected && <p>기록을 선택하면 상세 내용이 표시됩니다.</p>}
    {selected && <article style={card}>
      <h4 style={{ margin: "0 0 6px" }}>{selected.target} · {selected.status_text}</h4><div style={{ color: muted, fontSize: 11 }}>배포 ID: {selected.deployment_id}</div>
      {selected.error && <p style={{ whiteSpace: "pre-wrap" }}>{selected.error}</p>}{selected.remedy && <p>조치: {selected.remedy}</p>}
      {link && <a href={link} target="_blank" rel="noreferrer">기록된 서비스 URL 열기 ↗</a>}
      {!!selected.warnings.length && <div style={{ marginTop: 12, color: "var(--vscode-editorWarning-foreground, #cca700)" }}><b>기록 당시의 안내</b><ul style={{ paddingLeft: 20 }}>{selected.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul></div>}
      <ol style={{ paddingLeft: 22 }}>{selected.events.map((event, i) => <li key={i} style={{ marginBottom: 10 }}><b>{event.title}</b><div style={{ color: muted, fontSize: 11 }}>{date(event.at)}</div>{event.detail && <div style={{ whiteSpace: "pre-wrap" }}>{event.detail}</div>}</li>)}</ol>
      <div style={{ borderTop: border, paddingTop: 10 }}><b>이전 버전으로 복귀</b>
        {selected.rollback.target && <div style={{ margin: "6px 0", fontFamily: "var(--vscode-editor-font-family, monospace)" }}>{selected.rollback.target}</div>}
        {!selected.rollback.available && <p style={{ color: muted }}>{selected.rollback.reason}</p>}
        {selected.rollback.available && state.review !== selected.key && <button onClick={() => onReview(selected.key)} disabled={busy || state.loading} style={button}>{state.busyKey === selected.key ? "롤백 처리 중…" : "롤백 대상 확인"}</button>}
        {state.review === selected.key && <div role="group" aria-label="롤백 실행 확인" style={card}><b>{selected.source === "ecs" ? "ECS 롤백 승인 · Level 3" : "로컬 롤백 승인"}</b><p><b>{selected.target}</b>를 위의 이전 버전으로 되돌립니다. 실행 중인 서비스에 영향을 줍니다.</p>{selected.source === "ecs" && <p>요청 후 ECS 서비스 안정화 상태를 확인해야 합니다.</p>}<div style={{ display: "flex", gap: 8 }}><button style={button} disabled={busy || state.loading} onClick={() => onConfirm(selected)}>승인하고 롤백</button><button style={button} disabled={busy} onClick={() => onReview("")}>취소</button></div></div>}
        {result && <p role="status" style={{ whiteSpace: "pre-wrap", color: result.ok ? "var(--vscode-charts-green, #4ec9b0)" : "var(--vscode-errorForeground, #f48771)" }}>{result.message}</p>}
      </div>
    </article>}
  </section>;
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
