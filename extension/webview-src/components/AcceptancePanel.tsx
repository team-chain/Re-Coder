/**
 * 완성 기준 테스트 결과(2.0.45) — 결과 화면에서 명세의 조건마다 통과·실패·확인 불가와 그 근거를 보여 준다.
 *
 * 값은 코어 result.spec.acceptance(code_test.Session.summary)에서만 나온다. 시험을 못 돌렸으면(테스트 끔·기존 프로젝트·
 * 앱을 띄우지 못함) 지어내지 않고 "자동 시험 안 함"과 그 이유를 쓴다.
 */
import React, { useState } from "react";
import { MarkGlyph } from "./ui/marks";
import { KIND_LABEL, SpecKind } from "./specModel";

export interface AcceptanceStep { method: string; path: string; status: number | null; response?: string; problem?: string }
export interface AcceptanceCriterion {
  id: string; text?: string; verdict: "passed" | "failed" | "unverifiable" | string;
  ms?: number; checks?: number; reason?: string; retries?: number; history?: string[];
  failure?: { step?: number; message?: string; request?: string; file?: string };
  evidence?: AcceptanceStep[];
}
export interface AcceptanceSummary {
  status: "passed" | "failed" | "unverifiable" | "skipped" | string;
  passed: number; failed: number; unverifiable: number; total: number; checks?: number; rounds?: number;
  criteria: AcceptanceCriterion[]; first?: { passed: number; failed: number }; note?: string;
}
export interface ResultSpec {
  goal?: string; file?: string; verified?: boolean;
  criteria?: Array<{ id: string; text: string; kind: SpecKind; check?: string }>;
  acceptance?: AcceptanceSummary | null;
}

const css = `
.rc-acc{margin:0 0 10px;border:1px solid var(--vscode-widget-border,var(--vscode-panel-border,#3b3b3b));border-radius:8px;overflow:hidden;font-size:12.5px}
.rc-acc-h{display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--vscode-editorWidget-background,#252526)}
.rc-acc-h b{font-size:13.5px}
.rc-acc-badge{margin-left:auto;display:inline-flex;align-items:center;height:22px;padding:0 9px;border-radius:99px;font-size:12px;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.rc-acc-badge.ok{background:rgba(63,185,80,.15);color:var(--vscode-testing-iconPassed,#3fb950)}
.rc-acc-badge.bad{background:rgba(248,81,73,.15);color:var(--vscode-testing-iconFailed,#f85149)}
.rc-acc-badge.mid{background:rgba(204,167,0,.14);color:var(--vscode-editorWarning-foreground,#cca700)}
.rc-acc-seg{display:flex;height:4px;background:rgba(128,128,128,.25)}
.rc-acc-seg i{display:block;height:100%}
.rc-acc-sub{padding:7px 12px;border-top:1px solid var(--vscode-panel-border,#333);color:var(--vscode-descriptionForeground,#999);font-size:12px;line-height:1.5}
.rc-acc-row{border-top:1px solid var(--vscode-panel-border,#333)}
.rc-acc-row > button{all:unset;box-sizing:border-box;width:100%;display:flex;align-items:flex-start;gap:9px;padding:8px 12px;cursor:pointer}
.rc-acc-row > button:hover{background:var(--vscode-list-hoverBackground,rgba(255,255,255,.04))}
.rc-acc-row > button:focus-visible{outline:2px solid var(--vscode-focusBorder,#3794ff);outline-offset:-2px}
.rc-acc-ic{flex:none;width:16px;height:16px;margin-top:1px;border-radius:50%;display:grid;place-items:center;font-size:10.5px;font-weight:700;color:#fff}
.rc-acc-ic.passed{background:var(--vscode-testing-iconPassed,#3fb950)}
.rc-acc-ic.failed{background:var(--vscode-testing-iconFailed,#f85149)}
.rc-acc-ic.unverifiable{background:transparent;border:1.5px solid var(--vscode-descriptionForeground,#888);color:var(--vscode-descriptionForeground,#888);box-sizing:border-box}
.rc-acc-tx{flex:1;min-width:0;line-height:1.5}
.rc-acc-tx .id{font-weight:600;margin-right:6px}
.rc-acc-tx .kind{color:var(--vscode-descriptionForeground,#999);margin-right:6px;font-size:11.5px}
.rc-acc-side{flex:none;display:flex;gap:6px;align-items:center;color:var(--vscode-descriptionForeground,#999);font-size:11.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.rc-acc-chip{padding:1px 7px;border-radius:99px;border:1px solid rgba(204,167,0,.45);color:var(--vscode-editorWarning-foreground,#cca700)}
.rc-acc-det{padding:2px 12px 10px 37px;display:flex;flex-direction:column;gap:7px}
.rc-acc-tl{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:11.5px}
.rc-acc-tl span{padding:1px 8px;border-radius:5px;border:1px solid var(--vscode-widget-border,#3c3c3c)}
.rc-acc-tl span.failed{color:var(--vscode-testing-iconFailed,#f85149);border-color:rgba(248,81,73,.4)}
.rc-acc-tl span.passed{color:var(--vscode-testing-iconPassed,#3fb950);border-color:rgba(63,185,80,.4)}
.rc-acc-ev{margin:0;padding:7px 9px;border-radius:6px;background:var(--vscode-textCodeBlock-background,rgba(0,0,0,.25));font:11.5px/1.6 var(--vscode-editor-font-family,monospace);white-space:pre-wrap;overflow-wrap:anywhere;max-height:200px;overflow:auto}
.rc-acc-ev .m{color:var(--vscode-textLink-foreground,#3794ff)}
.rc-acc-ev .bad{color:var(--vscode-testing-iconFailed,#f85149)}
.rc-acc-ev .dim{color:var(--vscode-descriptionForeground,#999)}
.rc-acc-msg{color:var(--vscode-testing-iconFailed,#f85149);line-height:1.5}
.rc-acc-ft{padding:7px 12px;border-top:1px solid var(--vscode-panel-border,#333);color:var(--vscode-descriptionForeground,#888);font-size:11.5px}
`;

const VERDICT_TEXT: Record<string, string> = { passed: "통과", failed: "실패", unverifiable: "확인 불가" };
const MARK: Record<string, React.ReactNode> = { passed: <MarkGlyph kind="ok" size={10} />, failed: <MarkGlyph kind="bad" size={10} />, unverifiable: <MarkGlyph kind="skip" size={10} /> };

/** 머리 배지(순수) — "6/6 통과", "4/6 통과 · 실패 2", 시험 안 함. */
export function acceptanceBadge(a: AcceptanceSummary | null | undefined, criteria: number): { text: string; tone: "ok" | "bad" | "mid" } {
  if (!a || a.status === "skipped") return { text: `자동 시험 안 함 · 조건 ${criteria}개`, tone: "mid" };
  if (a.failed) return { text: `${a.passed}/${a.total} 통과 · 실패 ${a.failed}`, tone: "bad" };
  if (a.unverifiable) return { text: `${a.passed}/${a.total} 통과 · 확인 불가 ${a.unverifiable}`, tone: "mid" };
  return { text: `${a.passed}/${a.total} 통과`, tone: "ok" };
}

/** 한 줄 요약(순수) — 처음엔 실패했다가 고친 뒤 통과했으면 그 과정을 숫자로. */
export function acceptanceStory(a: AcceptanceSummary | null | undefined): string {
  if (!a || a.status === "skipped") return a?.note || "이 결과는 테스트하지 않았습니다. 적용 후 직접 확인해 주세요.";
  const parts = [`시험 ${a.checks ?? 0}개를 실행 중인 앱에 보내 확인했습니다`];
  if ((a.rounds ?? 1) > 1 && a.first) {
    parts.push(`처음 ${a.first.passed}/${a.total} 통과 → 실패를 고친 뒤 ${a.passed}/${a.total}`);
  }
  if (a.unverifiable) parts.push(`확인 불가 ${a.unverifiable}개는 요청만으로 확인할 수 없는 조건이라 직접 확인이 필요합니다`);
  return parts.join(" · ");
}

function msText(ms?: number): string {
  if (ms === undefined) return "";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}초` : `${ms}ms`;
}

function Row({ c, kind, check, open, onToggle }: { c: AcceptanceCriterion; kind?: SpecKind; check?: string; open: boolean; onToggle: () => void }) {
  const v = c.verdict in VERDICT_TEXT ? c.verdict : "unverifiable";
  const history = c.history ?? [];
  return (
    <div className="rc-acc-row" data-criterion={c.id} data-verdict={v}>
      <button type="button" aria-expanded={open} onClick={onToggle}>
        <span className={`rc-acc-ic ${v}`} aria-label={VERDICT_TEXT[v]}>{MARK[v]}</span>
        <span className="rc-acc-tx">
          <span className="id">{c.id}</span>
          {kind && <span className="kind">{KIND_LABEL[kind] ?? "기능"}</span>}
          {c.text}
        </span>
        <span className="rc-acc-side">
          {!!c.retries && <span className="rc-acc-chip" title="처음엔 실패했다가 코드를 고친 뒤 다시 시험했습니다">고친 뒤 · 재시도 {c.retries}</span>}
          {c.ms !== undefined && <span>{msText(c.ms)}</span>}
          <span aria-hidden="true">{open ? "▾" : "▸"}</span>
        </span>
      </button>
      {open && (
        <div className="rc-acc-det">
          {history.length > 1 && (
            <div className="rc-acc-tl" aria-label="시험 과정">
              {history.map((h, i) => (
                <React.Fragment key={i}>
                  {i > 0 && <span style={{ border: 0, padding: 0 }} aria-hidden="true">→ 고침 →</span>}
                  <span className={h}>{i + 1}차 {VERDICT_TEXT[h] ?? h}</span>
                </React.Fragment>
              ))}
            </div>
          )}
          {v === "unverifiable" && <div className="rc-acc-tx" style={{ color: "var(--vscode-descriptionForeground,#999)" }}>
            {c.reason || "요청으로 확인할 수 없는 조건입니다."}{check ? ` — 직접 확인: ${check}` : ""}</div>}
          {c.failure?.message && <div className="rc-acc-msg">{c.failure.step ? `${c.failure.step}단계 · ` : ""}{c.failure.request ? `${c.failure.request} — ` : ""}{c.failure.message}
            {c.failure.file ? <span style={{ color: "var(--vscode-descriptionForeground,#999)" }}> (원인 후보: {c.failure.file})</span> : null}</div>}
          {(c.evidence ?? []).length > 0 && (
            <pre className="rc-acc-ev" data-testid="acceptance-evidence">
              {(c.evidence ?? []).map((s, i) => (
                <React.Fragment key={i}>
                  <span className="m">{s.method}</span> {s.path} <span className={s.problem ? "bad" : "dim"}>→ {s.status ?? "응답 없음"}</span>{"\n"}
                  {s.response ? <span className="dim">  {s.response}{"\n"}</span> : null}
                  {s.problem ? <span className="bad">  {s.problem}{"\n"}</span> : null}
                </React.Fragment>
              ))}
            </pre>
          )}
          {v === "passed" && !(c.evidence ?? []).length && <div className="rc-acc-tx">시험 {c.checks ?? 1}개 통과</div>}
        </div>
      )}
    </div>
  );
}

export const AcceptancePanel: React.FC<{ spec: ResultSpec }> = ({ spec }) => {
  const criteria = spec.criteria ?? [];
  const a = spec.acceptance ?? null;
  const ran = !!a && a.status !== "skipped" && (a.criteria ?? []).length > 0;
  //: 실패한 조건은 처음부터 펼쳐 둔다 — 무엇이 왜 틀렸는지 바로 보이게.
  const [open, setOpen] = useState<Record<string, boolean>>(() =>
    Object.fromEntries((a?.criteria ?? []).filter((c) => c.verdict === "failed").map((c) => [c.id, true])));
  const badge = acceptanceBadge(a, criteria.length);
  const byId = new Map(criteria.map((c) => [c.id, c]));
  const rows: AcceptanceCriterion[] = ran ? a!.criteria : criteria.map((c) => ({ id: c.id, text: c.text, verdict: "unverifiable", reason: a?.note }));
  const total = ran ? a!.total || rows.length : 0;
  return (
    <section className="rc-acc" data-testid="code-spec-summary" aria-label="완성 기준 테스트">
      <style>{css}</style>
      <div className="rc-acc-h">
        <b>완성 기준 테스트</b>
        <span style={{ color: "var(--vscode-descriptionForeground,#999)", fontSize: 12 }}>조건 {criteria.length || rows.length}개</span>
        <span className={`rc-acc-badge ${badge.tone}`} data-testid="acceptance-badge">{badge.text}</span>
      </div>
      {ran && total > 0 && (
        <div className="rc-acc-seg" aria-hidden="true">
          <i style={{ width: `${(a!.passed / total) * 100}%`, background: "var(--vscode-testing-iconPassed,#3fb950)" }} />
          <i style={{ width: `${(a!.failed / total) * 100}%`, background: "var(--vscode-testing-iconFailed,#f85149)" }} />
          <i style={{ width: `${(a!.unverifiable / total) * 100}%`, background: "var(--vscode-descriptionForeground,#888)" }} />
        </div>
      )}
      <div className="rc-acc-sub" data-testid="acceptance-story">{acceptanceStory(a)}</div>
      {rows.map((c) => {
        const meta = byId.get(c.id);
        return <Row key={c.id} c={{ ...c, text: c.text || meta?.text }} kind={meta?.kind} check={meta?.check}
          open={!!open[c.id]} onToggle={() => setOpen((o) => ({ ...o, [c.id]: !o[c.id] }))} />;
      })}
      {spec.file && <div className="rc-acc-ft">기록: {spec.file}{spec.verified ? " · 모든 조건 통과로 표시됨" : ""}</div>}
    </section>
  );
};

export default AcceptancePanel;
