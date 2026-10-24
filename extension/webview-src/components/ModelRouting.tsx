/**
 * 작업별 AI 공급자 · 비용 원장 · 공급자 비교 (2.0.36)
 *
 * - 품질(코드 생성·보안 수정·교정)은 기본 공급자, 절약(설계 질문 주제·요약·오류 분류·테스트 초안)은 고른 공급자.
 * - 30일 비용을 등급·공급자·모델별로. 단가를 모르는 호출은 0 달러가 아니라 "단가 미설정"으로.
 * - 같은 요청을 두 공급자로 돌려 비용·교정 횟수·남은 오류·보안 점수를 나란히 보여 준다. 종합 승자는 정하지 않는다.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { MarkGlyph } from "./ui/marks";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import {
  CompareJob, ECONOMY_OPTIONS, ModelsState, OPERATION_LABEL, TIER_LABEL, compareChoices, compareRows, costCell, formatTokens,
  formatUsd, providerName,
} from "./modelRoutingModel";

const C = {
  fg: "var(--vscode-foreground, #e0e0e0)",
  muted: "var(--vscode-descriptionForeground, #8f98a3)",
  line: "var(--vscode-panel-border, #2e3238)",
  card: "var(--vscode-editorWidget-background, #202326)",
  ok: "var(--vscode-testing-iconPassed, #3ddc97)",
  warn: "var(--vscode-editorWarning-foreground, #f7b955)",
  err: "var(--vscode-editorError-foreground, #ff6573)",
  btn: "var(--vscode-button-background, #0e639c)",
  btnFg: "var(--vscode-button-foreground, #fff)",
};
const btn = (primary: boolean, enabled = true): React.CSSProperties => ({
  border: primary ? "1px solid transparent" : `1px solid ${C.line}`, borderRadius: 6, padding: "6px 12px", fontSize: 11.5, fontWeight: 600,
  cursor: enabled ? "pointer" : "default", background: primary ? C.btn : "transparent", color: primary ? C.btnFg : C.fg, opacity: enabled ? 1 : 0.5,
  fontFamily: "inherit",
});
const card: React.CSSProperties = { border: `1px solid ${C.line}`, borderRadius: 10, background: C.card, padding: "14px 16px", marginBottom: 14 };
const h: React.CSSProperties = { fontSize: 13.5, fontWeight: 700, marginBottom: 4 };
const sub: React.CSSProperties = { color: C.muted, fontSize: 11.5, lineHeight: 1.55, marginBottom: 10 };
const select: React.CSSProperties = {
  background: "var(--vscode-dropdown-background, #2b2b2b)", color: "var(--vscode-dropdown-foreground, #ddd)",
  border: `1px solid ${C.line}`, borderRadius: 5, padding: "5px 6px", fontSize: 11.5, fontFamily: "inherit", maxWidth: "100%",
};
const th: React.CSSProperties = { padding: "5px 6px", fontWeight: 500, color: C.muted, textAlign: "left", whiteSpace: "nowrap" };
const td: React.CSSProperties = { padding: "6px", borderTop: `1px solid ${C.line}`, verticalAlign: "top" };

const TierCard: React.FC<{ tier: "quality" | "economy"; provider: string; model: string; children?: React.ReactNode }> = ({ tier, provider, model, children }) => (
  <div data-tier={tier} style={{ flex: "1 1 220px", minWidth: 0, border: `1px solid ${C.line}`, borderRadius: 8, padding: "10px 12px" }}>
    <div style={{ fontSize: 11, color: C.muted }}>{TIER_LABEL[tier]} 등급</div>
    <div style={{ fontWeight: 700, marginTop: 2 }}>{providerName(provider)}</div>
    <div style={{ fontSize: 11, color: C.muted, wordBreak: "break-all" }}>{model}</div>
    {children}
  </div>
);

export const ModelRouting: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [state, setState] = useState<ModelsState | null>(null);
  const [error, setError] = useState("");
  const [job, setJob] = useState<CompareJob | null>(null);
  const [compareError, setCompareError] = useState("");
  const [starting, setStarting] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [pair, setPair] = useState<[string, string]>(["", ""]);
  const poll = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const seenRunning = useRef<string>("");

  const load = useCallback(() => { setError(""); postMessage("models.load"); }, [postMessage]);
  useEffect(() => { load(); return () => clearTimeout(poll.current); }, [load]);

  useMessage((m) => {
    if (m.type === "models.state") {
      const next = m.payload as ModelsState;
      setState(next);
      if (next.compare) { setJob(next.compare); }
      const choices = compareChoices(next.routing);
      setPair((p) => (p[0] && p[1] ? p : [choices[0]?.spec ?? "", choices[1]?.spec ?? ""]));
    } else if (m.type === "models.error") {
      setError(String((m.payload as { message?: string })?.message || "AI 공급자 정보를 받지 못했습니다."));
    } else if (m.type === "models.compare") {
      setStarting(false);
      setCompareError("");
      setJob(m.payload as CompareJob);
    } else if (m.type === "models.compareError") {
      setStarting(false);
      setCompareError(String((m.payload as { message?: string })?.message || ""));
    }
  });

  //: 진행 중이면 3초마다 상태를 묻는다. 끝나면 비용 원장도 새로 읽는다(비교 호출도 원장에 남는다).
  useEffect(() => {
    clearTimeout(poll.current);
    if (job?.status === "running") {
      seenRunning.current = job.id;
      poll.current = setTimeout(() => postMessage("models.compare.poll", { id: job.id }), 3000);
    } else if (job?.status === "finished" && seenRunning.current === job.id) {
      seenRunning.current = "";  // 한 번만
      postMessage("models.load");
    }
    return () => clearTimeout(poll.current);
  }, [job, postMessage]);

  if (error) {
    return <div style={card}><div style={h}>AI 공급자 · 비용</div><div style={{ color: C.err, fontSize: 12 }}>{error}</div>
      <button style={{ ...btn(false), marginTop: 10 }} onClick={load}>다시 읽기</button></div>;
  }
  if (!state) { return <div style={{ ...card, color: C.muted, fontSize: 12 }}>AI 공급자 정보를 읽는 중…</div>; }

  return <ModelRoutingView state={state} job={job} compareError={compareError} starting={starting} instruction={instruction}
    pair={pair} post={postMessage} onReload={load} onInstruction={setInstruction} onPair={setPair}
    onStart={() => { setStarting(true); setCompareError(""); postMessage("models.compare.start", { instruction: instruction.trim(), providers: pair }); }} />;
};

export interface ModelRoutingViewProps {
  state: ModelsState; job: CompareJob | null; compareError: string; starting: boolean; instruction: string; pair: [string, string];
  post: (type: string, payload?: unknown) => void; onReload: () => void; onInstruction: (v: string) => void;
  onPair: (f: (p: [string, string]) => [string, string]) => void; onStart: () => void;
}

/** 화면 본체 — 상태는 위 컨테이너가 갖고, 이쪽은 그리기만 한다(Node 에서 렌더 검사). */
export const ModelRoutingView: React.FC<ModelRoutingViewProps> = ({ state, job, compareError, starting, instruction, pair, post, onReload, onInstruction, onPair, onStart }) => {
  const postMessage = post;
  const load = onReload;
  const setInstruction = onInstruction;
  const setPair = onPair;
  const { routing, costs } = state;
  const choices = compareChoices(routing);
  const running = job?.status === "running";
  const canStart = !running && !starting && instruction.trim().length > 0 && pair[0] && pair[1] && pair[0] !== pair[1];
  const rows = job ? compareRows(job) : [];

  return (
    <div className="rc-models" style={{ fontSize: 12, lineHeight: 1.5 }}>
      <section style={card} aria-label="작업 등급">
        <div style={h}>작업 등급별 AI 공급자</div>
        <div style={sub}>코드 생성·보안 수정·교정은 품질 공급자가, 사람이 다시 확인하는 가벼운 일은 절약 공급자가 맡습니다. 절약 공급자가 실패하면 품질 공급자가 이어 받습니다.</div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <TierCard tier="quality" provider={routing.quality.provider} model={routing.quality.model}>
            <div style={{ fontSize: 11, color: C.muted, marginTop: 6 }}>코드 생성 · 보안 수정 · 교정 · 그 밖의 모든 작업</div>
          </TierCard>
          <TierCard tier="economy" provider={routing.economy.provider} model={routing.economy.model}>
            <div style={{ fontSize: 11, color: C.muted, marginTop: 6 }}>{routing.economy_operations.map((o) => OPERATION_LABEL[o] ?? o).join(" · ")}</div>
            {routing.economy.scrubbed && <div data-testid="economy-scrubbed" style={{ fontSize: 11, color: C.ok, marginTop: 4 }}>다른 공급자로 나가는 문맥은 비밀값을 가립니다</div>}
            {!routing.economy.separate && <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>지금은 품질 공급자와 같습니다(기존 동작).</div>}
            {routing.economy.note && <div style={{ fontSize: 11, color: C.warn, marginTop: 4 }}>{routing.economy.note}</div>}
          </TierCard>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 12 }}>
          <label htmlFor="rc-economy" style={{ color: C.muted }}>절약 공급자</label>
          <select id="rc-economy" style={select} value={state.settings.economyProvider}
            onChange={(e) => postMessage("models.setEconomy", { provider: e.target.value })}>
            {ECONOMY_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}{state.keys && o.value && o.value !== "fast" && !state.keys[o.value] ? " — 키 입력 필요" : ""}</option>)}
          </select>
          <span style={{ color: C.muted, fontSize: 11 }}>바꾸면 키를 묻고(필요할 때) Core를 다시 시작합니다.</span>
        </div>
      </section>

      <section style={card} aria-label="비용 원장">
        <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
          <div style={h}>비용 원장 · 최근 {costs.days}일</div>
          <span style={{ fontWeight: 700 }}>{formatUsd(costs.total_cost_usd)}</span>
          {costs.unknown_cost_calls > 0 && <span style={{ color: C.warn }}>단가 미설정 {costs.unknown_cost_calls}회</span>}
          {costs.economy_saved_usd > 0 && <span style={{ color: C.ok }}>절약 등급으로 아낀 비용 {formatUsd(costs.economy_saved_usd)}</span>}
        </div>
        <div style={sub}>호출 {costs.calls}회. 프롬프트·응답 본문은 남기지 않고 숫자와 이름만 적습니다.{costs.persisted ? "" : " (이 실행의 기록만)"}</div>
        {costs.groups.length === 0 ? <div style={{ color: C.muted }}>아직 기록이 없습니다.</div> : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11.5 }}>
              <thead><tr><th style={th}>등급</th><th style={th}>공급자 · 모델</th><th style={th}>호출</th><th style={th}>토큰(입력/출력)</th><th style={th}>비용</th></tr></thead>
              <tbody>{costs.groups.map((g) => (
                <tr key={`${g.tier}/${g.provider}/${g.model}`} data-cost-row={g.tier}>
                  <td style={td}>{TIER_LABEL[g.tier] ?? g.tier}</td>
                  <td style={{ ...td, wordBreak: "break-all" }}>{providerName(g.provider)}<div style={{ color: C.muted, fontSize: 10.5 }}>{g.model}</div></td>
                  <td style={td}>{g.calls}{g.masked > 0 && <div style={{ color: C.muted, fontSize: 10.5 }}>가림 {g.masked}곳</div>}</td>
                  <td style={td}>{formatTokens(g.input_tokens)} / {formatTokens(g.output_tokens)}{g.estimated_token_calls > 0 && <div style={{ color: C.muted, fontSize: 10.5 }}>어림 {g.estimated_token_calls}회</div>}</td>
                  <td style={{ ...td, color: g.unknown_cost_calls ? C.warn : C.fg }}>{costCell(g.unknown_cost_calls === g.calls ? null : g.cost_usd, g.unknown_cost_calls)}</td>
                </tr>))}
              </tbody>
            </table>
          </div>
        )}
        <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          <button style={btn(false)} onClick={() => postMessage("models.openPrices")}>모델 단가 설정</button>
          <button style={btn(false)} onClick={load}>새로 읽기</button>
        </div>
      </section>

      <section style={card} aria-label="공급자 비교">
        <div style={h}>같은 요청을 두 공급자로 비교</div>
        <div style={sub}>실제 코드 생성 경로를 공급자만 바꿔 두 번 돌립니다. 파일은 바꾸지 않습니다. AI 요금이 두 번 들고 몇 분 걸릴 수 있습니다. 한쪽이 실패해도 다른 공급자로 바꾸지 않습니다.</div>
        <textarea aria-label="비교할 요청" value={instruction} onChange={(e) => setInstruction(e.target.value)} maxLength={4000} rows={3}
          placeholder="예: 할 일 목록 API 와 화면을 만들어 주세요" disabled={running}
          style={{ width: "100%", boxSizing: "border-box", resize: "vertical", fontFamily: "inherit", fontSize: 12, padding: 8, borderRadius: 6,
            border: `1px solid ${C.line}`, background: "var(--vscode-input-background, #1e1e1e)", color: "var(--vscode-input-foreground, #ddd)" }} />
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 8 }}>
          {[0, 1].map((i) => (
            <select key={i} aria-label={`비교 공급자 ${i + 1}`} style={select} value={pair[i]} disabled={running}
              onChange={(e) => setPair((p) => (i === 0 ? [e.target.value, p[1]] : [p[0], e.target.value]))}>
              <option value="">공급자 고르기</option>
              {choices.map((c) => <option key={c.spec} value={c.spec}>{c.label} — {providerName(c.provider)} {c.model}</option>)}
            </select>
          ))}
          <button style={btn(true, Boolean(canStart))} disabled={!canStart}
            onClick={onStart}>
            {running ? "비교 중…" : starting ? "확인 중…" : "비교 시작"}
          </button>
        </div>
        {choices.length < 2 && <div style={{ color: C.muted, fontSize: 11, marginTop: 6 }}>비교하려면 공급자가 두 개 필요합니다. 절약 공급자로 다른 공급자를 고르면 그 공급자도 여기서 고를 수 있습니다.</div>}
        {compareError && <div role="alert" style={{ color: C.err, fontSize: 11.5, marginTop: 8 }}>{compareError}</div>}
        {job && (
          <div data-testid="compare-result" style={{ marginTop: 12, overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11.5 }}>
              <thead><tr><th style={th}>항목</th>{job.sides.map((s) => (
                <th key={s.spec} style={th} data-side-status={s.status}>{providerName(s.provider)}<div style={{ fontWeight: 400, fontSize: 10.5, wordBreak: "break-all" }}>{s.model}</div>
                  <div style={{ fontWeight: 400, fontSize: 10.5, color: s.status === "failed" ? C.err : C.muted }}>
                    {{ waiting: "기다리는 중", running: "만드는 중…", succeeded: "완료", failed: "실패" }[s.status]}</div></th>))}</tr></thead>
              <tbody>{rows.map((r) => (
                <tr key={r.key} data-row={r.key}>
                  <td style={{ ...td, color: C.muted, whiteSpace: "nowrap" }}>{r.label}</td>
                  {r.values.map((v, i) => <td key={i} style={{ ...td, fontWeight: r.better === i ? 700 : 400, color: r.better === i ? C.ok : C.fg }} data-better={r.better === i ? "true" : undefined}>{v}{r.better === i ? <> <MarkGlyph kind="ok" size={11} label="더 나음" /></> : ""}</td>)}
                </tr>))}
              </tbody>
            </table>
            {job.sides.filter((s) => s.error).map((s) => <div key={s.spec} style={{ color: C.err, fontSize: 11, marginTop: 6, wordBreak: "break-word" }}>{providerName(s.provider)}: {s.error}</div>)}
            {job.status === "finished" && <div style={{ color: C.muted, fontSize: 10.5, marginTop: 8 }}>초록 표시는 그 항목에서 더 나은 쪽입니다. 종합 판단은 결과 코드를 직접 보고 하세요 — 한 번의 비교는 우연의 영향을 받습니다.</div>}
          </div>
        )}
      </section>
    </div>
  );
};

export default ModelRouting;
