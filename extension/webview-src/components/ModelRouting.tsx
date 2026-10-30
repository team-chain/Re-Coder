/**
 * 작업별 AI 공급자 · 비용 원장 · 공급자 비교 (2.0.36)
 *
 * - 품질(코드 생성·보안 수정·교정)은 기본 공급자, 절약(설계 질문 주제·요약·오류 분류·테스트 초안)은 고른 공급자.
 * - 30일 비용을 등급·공급자·모델별로. 단가를 모르는 호출은 0 달러가 아니라 "단가 미설정"으로.
 * - 같은 요청을 두 공급자로 돌려 비용·교정 횟수·남은 오류·보안 점수를 나란히 보여 준다. 종합 승자는 정하지 않는다.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Switch } from "./Check";
import { RecommendationIcon } from "./TeamSetup";
import { prettyModel } from "./teamConfig";
import { MarkGlyph } from "./ui/marks";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import {
  CompareJob, ModelsState, TIER_LABEL, compareChoices, compareRows, costCell, formatTokens,
  formatUsd, providerName, modelPrice, modelWarning, tierCost,
} from "./modelRoutingModel";

const C = {
  fg: "var(--rc-fg, var(--vscode-foreground))",
  muted: "var(--rc-fg2, var(--vscode-descriptionForeground))",
  line: "var(--rc-line, var(--vscode-panel-border))",
  card: "var(--rc-tile, var(--vscode-editorWidget-background))",
  ok: "var(--rc-ok, var(--vscode-testing-iconPassed))",
  warn: "var(--rc-warn, var(--vscode-editorWarning-foreground))",
  err: "var(--vscode-editorError-foreground, #ff6573)",
  btn: "var(--vscode-button-background, #0e639c)",
  btnFg: "var(--vscode-button-foreground, #fff)",
};
const btn = (primary: boolean, enabled = true): React.CSSProperties => ({
  border: primary ? "1px solid transparent" : `1px solid ${C.line}`, borderRadius: 6, padding: "6px 12px", fontSize: 11.5, fontWeight: 600,
  cursor: enabled ? "pointer" : "default", background: primary ? C.btn : "transparent", color: primary ? C.btnFg : C.fg, opacity: enabled ? 1 : 0.5,
  fontFamily: "inherit",
});
const card: React.CSSProperties = { border: `1px solid ${C.line}`, borderRadius: 18, background: C.card, padding: "20px 22px", marginBottom: 16 };
const h: React.CSSProperties = { fontSize: 15, fontWeight: 700, marginBottom: 4 };
const sub: React.CSSProperties = { color: C.muted, fontSize: 11.5, lineHeight: 1.55, marginBottom: 10 };
const select: React.CSSProperties = {
  background: "var(--vscode-dropdown-background, #2b2b2b)", color: "var(--vscode-dropdown-foreground, #ddd)",
  border: `1px solid ${C.line}`, borderRadius: 5, padding: "5px 6px", fontSize: 11.5, fontFamily: "inherit", maxWidth: "100%",
};
const th: React.CSSProperties = { padding: "5px 6px", fontWeight: 500, color: C.muted, textAlign: "left", whiteSpace: "nowrap" };
const td: React.CSSProperties = { padding: "6px", borderTop: `1px solid ${C.line}`, verticalAlign: "top" };

export const ModelRouting: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [state, setState] = useState<ModelsState | null>(null);
  const [error, setError] = useState("");
  const [job, setJob] = useState<CompareJob | null>(null);
  const [compareError, setCompareError] = useState("");
  const [starting, setStarting] = useState(false);
  const [busy, setBusy] = useState(false);
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
      setBusy(false);
      if (next.compare) { setJob(next.compare); }
      const choices = compareChoices(next.routing);
      setPair((p) => (p[0] && p[1] ? p : [choices[0]?.spec ?? "", choices[1]?.spec ?? ""]));
    } else if (m.type === "models.saved") {
      load();
    } else if (m.type === "models.error") {
      setBusy(false);
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

  if (!state) { return <div style={{ ...card, color: C.muted, fontSize: 12 }}>{error || "AI 모델 정보를 읽는 중…"}{error && <button style={btn(false)} onClick={load}>다시 읽기</button>}</div>; }

  return <ModelRoutingView state={state} job={job} compareError={compareError} starting={starting} instruction={instruction}
    pair={pair} busy={busy} error={error} post={(type, payload) => {
      if (["models.check", "models.setModels", "models.setToggle"].includes(type)) { setBusy(true); setError(""); }
      postMessage(type, payload);
    }} onReload={load} onInstruction={setInstruction} onPair={setPair}
    onStart={() => { setStarting(true); setCompareError(""); postMessage("models.compare.start", { instruction: instruction.trim(), providers: pair }); }} />;
};

export interface ModelRoutingViewProps {
  busy?: boolean; error?: string;
  state: ModelsState; job: CompareJob | null; compareError: string; starting: boolean; instruction: string; pair: [string, string];
  post: (type: string, payload?: unknown) => void; onReload: () => void; onInstruction: (v: string) => void;
  onPair: (f: (p: [string, string]) => [string, string]) => void; onStart: () => void;
}

/** 화면 본체 — 상태는 위 컨테이너가 갖고, 이쪽은 그리기만 한다(Node 에서 렌더 검사). */
export const ModelRoutingView: React.FC<ModelRoutingViewProps> = ({ state, job, compareError, starting, instruction, pair, post, onReload, onInstruction, onPair, onStart, busy = false, error = "" }) => {
  const [showCompare, setShowCompare] = useState(!!job);
  const postMessage = post;
  const load = onReload;
  const setInstruction = onInstruction;
  const setPair = onPair;
  const { routing, costs } = state;
  const choices = compareChoices(routing);
  const running = job?.status === "running";
  const canStart = !running && !starting && instruction.trim().length > 0 && pair[0] && pair[1] && pair[0] !== pair[1];
  const rows = job ? compareRows(job) : [];
  const qualityModel = state.settings.qualityModel || routing.choices.find(c => c.spec === "quality")?.model || routing.quality.model;
  const fastModel = state.settings.fastModel || routing.choices.find(c => c.spec === "fast")?.model || qualityModel;
  const catalog = routing.catalog || [...new Set([qualityModel, fastModel])].filter(Boolean).map(model => ({ model, ready: false, checked: false, retirement: undefined }));
  const warning = modelWarning(state);
  const checked = routing.checked_at ? new Date(routing.checked_at).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false }) : "";
  const toggles = [
    { key: "autoRecommend" as const, title: "요청마다 추천 적용", description: "작음 → 비용 절약 · 중간 → 균형 · 큼 → 품질 우선" },
    { key: "lockSensitive" as const, title: "인증·결제 요청은 설계·개발 고정", description: state.settings.lockSensitive === false ? "꺼 두면 이런 요청도 간단한 모델을 고를 수 있습니다" : "로그인·결제·개인정보가 들어가면 복잡한 모델만 씁니다" },
    { key: "retryWithComplex" as const, title: "실패하면 복잡한 모델로 다시 만들기", description: "간단한 모델의 결과가 빌드·보안 검사에서 실패하면 1회" },
  ];

  return (
    <div className="rc-models" style={{ fontSize: 12, lineHeight: 1.5, maxWidth: 720, margin: "0 auto" }}>
      {error && <div role="alert" style={{ color: C.err, marginBottom: 12 }}>{error}</div>}
      <section style={card} aria-label="모델">
        <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 10 }}>
          <h2 style={{ ...h, margin: 0 }}>모델</h2>
          <span style={{ marginLeft: "auto", color: C.muted, fontSize: 12 }}>{providerName(routing.quality.provider)} · {checked ? `${checked} 확인` : "확인 전"}</span>
          <button style={btn(false, !busy)} disabled={busy} onClick={() => postMessage("models.check")}>{busy ? "적용 중…" : "모델 확인"}</button>
        </div>
        {(["quality", "fast"] as const).map(tier => <div key={tier} data-tier={tier} style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: "8px 14px", marginTop: 14 }}>
          <label htmlFor={`rc-model-${tier}`} style={{ width: 92, fontSize: 13.5, fontWeight: 600 }}>{tier === "quality" ? "복잡한 작업" : "간단한 작업"}</label>
          <select id={`rc-model-${tier}`} style={{ ...select, width: 220, height: 36, borderRadius: 10, fontSize: 13 }}
            value={tier === "quality" ? qualityModel : fastModel} disabled={busy || routing.configurable === false}
            onChange={e => postMessage("models.setModels", { tier, model: e.target.value })}>
            {catalog.map(m => <option key={m.model} value={m.model} disabled={!m.ready || !m.checked || !!m.retirement}>
              {prettyModel(m.model)}{m.retirement ? ` — ${m.retirement} 종료` : !m.checked ? " — 확인 필요" : !m.ready ? " — 쓸 수 없음" : ""}
            </option>)}
          </select>
          <span style={{ color: C.muted, fontSize: 12 }} title="100만 토큰당 입력 / 출력 단가">{modelPrice(state.settings.prices, tier === "quality" ? qualityModel : fastModel)} · {tier === "quality" ? "코드 생성 · 보안 수정 · 교정" : "요약 · 오류 분류 · 테스트 초안"}</span>
        </div>)}
        {warning && <div role="status" style={{ color: C.warn, marginTop: 12 }}>{warning}</div>}
      </section>
      <section style={card} aria-label="추천 · 안전장치">
        <h2 style={{ ...h, margin: 0 }}>추천 · 안전장치</h2>
        {toggles.map(t => <div key={t.key} style={{ display: "flex", alignItems: "center", gap: 12, padding: "12px 0", borderBottom: `1px solid ${C.line}` }}>
          <div style={{ flex: 1, minWidth: 0 }}><b style={{ display: "block", fontSize: 13.5 }}>{t.title}</b>
            <span style={{ display: "block", marginTop: 2, fontSize: 12, color: t.key === "lockSensitive" && state.settings.lockSensitive === false ? C.warn : C.muted }}>{t.description}</span>
          </div>
          <Switch checked={state.settings[t.key] !== false} disabled={busy} ariaLabel={t.title}
            onChange={value => postMessage("models.setToggle", { key: t.key, value })}/>
        </div>)}
        <div style={{ display: "flex", alignItems: "center", gap: 12, paddingTop: 12 }}>
          <div style={{ flex: 1 }}><b style={{ display: "block", fontSize: 13.5 }}>보안 수정 · 교정은 항상 복잡한 모델</b><span style={{ color: C.muted }}>끌 수 없습니다</span></div>
          <span style={{ color: C.muted, paddingRight: 9 }}><RecommendationIcon locked /></span>
        </div>
      </section>
      <section style={card} aria-label="최근 30일 비용">
        <div style={{ display: "flex", alignItems: "baseline", flexWrap: "wrap", gap: 10 }}>
          <h2 style={{ ...h, margin: 0 }}>최근 30일 비용</h2>
          <span style={{ marginLeft: "auto", color: C.ok, fontSize: 12.5 }}>간단한 모델로 {formatUsd(costs.economy_saved_usd)} 아낌</span>
        </div>
        <div style={{ fontSize: 26, fontWeight: 700, marginTop: 8 }}>{costCell(costs.calls > 0 && costs.unknown_cost_calls === costs.calls ? null : costs.total_cost_usd, costs.unknown_cost_calls)}</div>
        {(["quality", "economy"] as const).map(tier => {
          const cost = tierCost(costs, tier);
          return <div key={tier} data-cost-row={tier} style={{ display: "flex", gap: 10, marginTop: 8, fontSize: 13 }}>
            <span style={{ flex: 1 }}>{tier === "quality" ? "복잡한 작업" : "간단한 작업"}</span><span style={{ color: C.muted }}>{cost.calls}회</span><span>{cost.text}</span>
          </div>;
        })}
        <button style={{ border: 0, background: "transparent", color: "var(--vscode-textLink-foreground)", padding: 0, cursor: "pointer", font: "inherit", marginTop: 14 }}
          aria-expanded={showCompare} onClick={() => setShowCompare(!showCompare)}>같은 요청을 두 모델로 비교</button>
        <details style={{ marginTop: 12, color: C.muted }}><summary style={{ cursor: "pointer" }}>비용 기록 자세히</summary>
          <div style={{ overflowX: "auto", marginTop: 10 }}><table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11.5 }}>
            <thead><tr><th style={th}>등급</th><th style={th}>공급자 · 모델</th><th style={th}>호출</th><th style={th}>토큰(입력/출력)</th><th style={th}>비용</th></tr></thead>
            <tbody>{costs.groups.map(g => <tr key={`${g.tier}/${g.provider}/${g.model}`}>
              <td style={td}>{TIER_LABEL[g.tier] ?? g.tier}</td><td style={td}>{providerName(g.provider)}<div>{g.model}</div></td>
              <td style={td}>{g.calls}{g.masked > 0 && <div>가림 {g.masked}곳</div>}</td><td style={td}>{formatTokens(g.input_tokens)} / {formatTokens(g.output_tokens)}</td>
              <td style={td}>{costCell(g.unknown_cost_calls === g.calls ? null : g.cost_usd, g.unknown_cost_calls)}</td>
            </tr>)}</tbody>
          </table></div>
          <button style={{ ...btn(false), marginTop: 10 }} onClick={() => postMessage("models.openPrices")}>모델 단가 설정</button>{" "}
          <button style={btn(false)} onClick={load}>새로 읽기</button>
        </details>
      </section>

      {showCompare && <section style={card} aria-label="공급자 비교">
        <div style={h}>같은 요청을 두 모델로 비교</div>
        <div style={sub}>같은 요청을 두 모델로 만듭니다. 보안 수정·교정과 민감 요청의 설계·개발에는 복잡한 모델을 씁니다. 파일은 바꾸지 않습니다. AI 요금이 두 번 들고 몇 분 걸릴 수 있습니다.</div>
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
      </section>}
    </div>
  );
};

export default ModelRouting;
