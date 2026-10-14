/**
 * 보안 검사 결과 아래에 붙는 "바로 고칠 수 있는 항목".
 * 고른 것만 적용 → 바뀐 부분 다시 검사(이미지 취약점은 이미지를 다시 빌드한 뒤 검사).
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Checkbox } from "./Check";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import type { ScanKind } from "./scanQueue";
import { FixApplyResult, FixProposal, defaultSelection, diffLineKind, fixReports, followUp } from "./securityFixes";

const C = {
  fg: "var(--vscode-foreground, #e0e0e0)",
  muted: "var(--vscode-descriptionForeground, #8f98a3)",
  line: "var(--vscode-panel-border, #2e3238)",
  btn: "var(--vscode-button-background, #0e639c)",
  btnFg: "var(--vscode-button-foreground, #fff)",
  warn: "var(--vscode-editorWarning-foreground, #cca700)",
  ok: "var(--vscode-gitDecoration-addedResourceForeground, #3fb950)",
  bad: "var(--vscode-gitDecoration-deletedResourceForeground, #f85149)",
};

type Phase = "idle" | "planning" | "ready" | "applying" | "rebuilding" | "rescanning";
interface ResultLike { requestId?: string; status?: string; findings?: unknown; workspace?: string }

/** 빌드 로그 끝의 의미 있는 두 줄(화면에 원인을 바로 보이게). */
const lastLines = (log: string) => {
  const lines = log.split("\n").map(l => l.trim()).filter(l => l && !/^#\d+ (DONE|CACHED)/.test(l));
  return lines.length ? `\n${lines.slice(-2).join("\n")}` : "";
};

const TOOL: Record<string, string> = { trivy: "이미지 · 의존성", hadolint: "Dockerfile", gitleaks: "시크릿" };

export const SecurityFixList: React.FC<{
  results: Partial<Record<ScanKind, ResultLike>>;
  running: boolean;
  onRescan: (kinds: ScanKind[]) => void;
}> = ({ results, running, onRescan }) => {
  const { postMessage, useMessage } = useVSCodeApi();
  const [phase, setPhase] = useState<Phase>("idle");
  const [proposals, setProposals] = useState<FixProposal[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [outcome, setOutcome] = useState<{ result: FixApplyResult; titles: string[]; after: string } | null>(null);
  const planned = useRef<{ key: string; workspace?: string; reports: Record<string, unknown> } | null>(null);
  const pending = useRef("");
  const applied = useRef<FixProposal[]>([]);

  //: 검사가 모두 끝나면(발견이 있을 때) 수정안을 받는다. 같은 결과로는 한 번만.
  useEffect(() => {
    if (running) return;
    const next = fixReports(results);
    if (!next) {
      if (planned.current) { planned.current = null; setProposals([]); if (phase !== "rescanning") setPhase("idle"); }
      if (phase === "rescanning") setPhase("idle");
      return;
    }
    if (planned.current?.key === next.key) { if (phase === "rescanning") setPhase("ready"); return; }
    planned.current = next;
    pending.current = `plan-${Date.now()}`;
    setPhase("planning"); setError("");
    postMessage("security.fixPlan", { requestId: pending.current, workspace: next.workspace, reports: next.reports });
  }, [results, running, phase, postMessage]);

  const rescan = useCallback((kinds: ScanKind[], after: string) => {
    setOutcome(cur => cur ? { ...cur, after } : cur);
    if (kinds.length) { setPhase("rescanning"); onRescan(kinds); } else { setPhase("ready"); }
  }, [onRescan]);

  useMessage(useCallback((msg) => {
    const p = (msg.payload ?? {}) as { requestId?: string } & Record<string, unknown>;
    if (!String(msg.type).startsWith("security.") || p.requestId !== pending.current) return;
    if (msg.type === "security.fixPlanResult") {
      const list = (p.proposals as FixProposal[]) ?? [];
      setProposals(list); setSelected(defaultSelection(list)); setPhase("ready");
    } else if (msg.type === "security.fixApplyResult") {
      const result = p as unknown as FixApplyResult;
      const done = applied.current.filter(x => result.applied.includes(x.id));
      setOutcome({ result, titles: done.map(x => x.title), after: "" });
      const next = followUp(done, results);
      if (next.rebuild) {
        pending.current = `rebuild-${Date.now()}`;
        setPhase("rebuilding");
        postMessage("security.rebuild", { requestId: pending.current, workspace: planned.current?.workspace });
        applied.current = done;
      } else {
        const trivyOnly = done.some(x => x.rebuild) && !next.rebuild;
        rescan(next.rescan, trivyOnly ? "이미지 관련 수정은 다음 배포에서 새로 빌드한 이미지로 다시 검사됩니다." : "");
      }
    } else if (msg.type === "security.rebuildResult") {
      const ok = p.status === "ok";
      const next = followUp(applied.current, results);
      rescan(ok ? [...next.rescan, "trivy"] : next.rescan,
        ok ? `${String(p.image ?? "이미지")} 를 다시 빌드해 이미지 검사를 다시 돌립니다.`
          : `이미지를 다시 빌드하지 못했습니다 — ${String(p.message ?? "")}${lastLines(String(p.log_tail ?? ""))}`);
    } else if (msg.type === "security.fixError") {
      setError(String(p.message ?? "요청이 실패했습니다.")); setPhase(proposals.length ? "ready" : "idle");
    }
  }, [results, postMessage, rescan, proposals.length]));

  const apply = () => {
    const ids = proposals.filter(p => p.auto && selected.has(p.id)).map(p => p.id);
    if (!ids.length || !planned.current) return;
    applied.current = proposals.filter(p => ids.includes(p.id));
    pending.current = `apply-${Date.now()}`;
    setPhase("applying"); setError(""); setOutcome(null);
    postMessage("security.fixApply", { requestId: pending.current, workspace: planned.current.workspace, reports: planned.current.reports, ids });
  };

  if (phase === "idle" && !outcome && !error) return null;
  const autos = proposals.filter(p => p.auto);
  const manual = proposals.filter(p => !p.auto);
  const busy = phase === "planning" || phase === "applying" || phase === "rebuilding" || phase === "rescanning";
  const count = autos.filter(p => selected.has(p.id)).length;
  const toggle = (id: string) => setSelected(cur => { const next = new Set(cur); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const status = phase === "planning" ? "수정안을 만드는 중…" : phase === "applying" ? "수정을 적용하는 중…"
    : phase === "rebuilding" ? "고친 내용으로 이미지를 다시 빌드하는 중…(몇 분 걸릴 수 있습니다)" : phase === "rescanning" ? "고친 부분을 다시 검사하는 중…" : "";

  return (
    <section className="rc-fixes" aria-busy={busy}>
      <style>{`
        .rc-fixes{border-top:1px solid ${C.line};margin-top:4px;padding-top:20px}
        .rc-fixes h3{font-size:13px;font-weight:600;margin:0 0 6px}.rc-fixes>p{font-size:11.5px;color:${C.muted};margin:0 0 14px;line-height:1.6}
        .rc-fix{display:grid;grid-template-columns:18px 1fr;align-items:start;gap:10px;padding:12px 0;border-top:1px solid ${C.line}}
        .rc-fix:first-of-type{border-top:none}.rc-fix input{margin:2px 0 0}
        .rc-fix b{font-size:12.5px;font-weight:600}.rc-fix small{color:${C.muted};font-size:10.5px;margin-left:8px}
        .rc-fix p{margin:4px 0 0;font-size:11.5px;color:${C.muted};line-height:1.6;overflow-wrap:anywhere}
        .rc-fix .risk{color:${C.warn}}
        .rc-fix details{margin-top:6px}.rc-fix summary{cursor:pointer;font-size:11px;color:${C.muted}}
        .rc-diff{margin:6px 0 0;padding:8px 10px;border:1px solid ${C.line};border-radius:6px;font:11px/1.5 var(--vscode-editor-font-family,Menlo,monospace);overflow:auto;max-height:260px;white-space:pre}
        .rc-diff .add{color:${C.ok}}.rc-diff .del{color:${C.bad}}.rc-diff .meta{color:${C.muted}}
        .rc-fix-manual{margin-top:12px}.rc-fix-manual>summary{cursor:pointer;font-size:12px;color:${C.muted};padding:6px 0}
        .rc-fix-actions{display:flex;align-items:center;gap:12px;margin-top:14px;flex-wrap:wrap}
        .rc-fix-actions button{border:1px solid transparent;border-radius:6px;padding:8px 14px;font-size:11.5px;font-weight:600;background:${C.btn};color:${C.btnFg};cursor:pointer}
        .rc-fix-actions button:disabled{opacity:.5;cursor:default}
        .rc-fix-out{margin-top:12px;font-size:11.5px;line-height:1.7;color:${C.muted}}.rc-fix-out b{color:${C.ok}}
      `}</style>
      <h3>바로 고칠 수 있는 항목{autos.length ? ` ${autos.length}개` : ""}</h3>
      <p>검사 결과로 만든 수정안입니다. 고른 것만 바꾸고 원본은 .recoder/backups 에 남깁니다. 적용한 뒤 바뀐 부분을 다시 검사합니다.</p>
      {phase === "ready" && !autos.length && !manual.length && <p style={{ fontSize: 11.5, color: C.muted }}>자동으로 고칠 수 있는 항목이 없습니다.</p>}
      {autos.map(p => (
        <div key={p.id} className="rc-fix">
          <Checkbox checked={selected.has(p.id)} disabled={busy} onChange={() => toggle(p.id)} ariaLabel={p.title} compact />
          <div>
            <b>{p.title}</b><small>{TOOL[p.tool] ?? p.tool}</small>
            <p>{p.detail}</p>
            {p.risk && <p className="risk">주의 · {p.risk}</p>}
            {p.note && <p>{p.note}</p>}
            {p.diff && <details><summary>변경 내용 보기{p.files.length ? ` · ${p.files.join(", ")}` : ""}</summary>
              <pre className="rc-diff">{p.diff.split("\n").map((line, i) => <div key={i} className={diffLineKind(line)}>{line || " "}</div>)}</pre></details>}
          </div>
        </div>
      ))}
      {manual.length > 0 && <details className="rc-fix-manual" open={!autos.length}>
        <summary>직접 확인할 것 {manual.length}개</summary>
        {manual.map(p => <div key={p.id} className="rc-fix" style={{ gridTemplateColumns: "1fr" }}><div><b>{p.title}</b><small>{TOOL[p.tool] ?? p.tool}</small><p>{p.detail}</p>{p.note && <p>{p.note}</p>}</div></div>)}
      </details>}
      <div className="rc-fix-actions">
        {autos.length > 0 && <button disabled={busy || running || count === 0} onClick={apply}>{phase === "applying" ? "적용 중…" : `선택한 ${count}개 적용`}</button>}
        <span role="status" style={{ fontSize: 11.5, color: error ? C.bad : C.muted }}>{error || status}</span>
      </div>
      {outcome && <div className="rc-fix-out" role="status">
        {outcome.result.applied.length > 0 && <div><b>적용 {outcome.result.applied.length}개</b> — {outcome.titles.join(", ")}</div>}
        {outcome.result.changed.length > 0 && <div>바뀐 파일: {outcome.result.changed.join(", ")}</div>}
        {outcome.result.skipped.map(s => <div key={s.id}>건너뜀: {proposals.find(p => p.id === s.id)?.title ?? s.id} — {s.reason}</div>)}
        {outcome.result.notes.map((n, i) => <div key={i}>{n}</div>)}
        {outcome.after && <div style={{ whiteSpace: "pre-line" }}>{outcome.after}</div>}
      </div>}
    </section>
  );
};
