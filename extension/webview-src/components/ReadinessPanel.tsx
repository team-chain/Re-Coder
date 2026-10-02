/**
 * 배포 준비 점검 · 빌드 실패 원인 — 로컬 Docker 배포(ShipMode)에서 쓴다.
 *
 * Core 의 build_readiness(정적 점검)와 build_failure(빌드 출력 진단) 결과를 그대로
 * 보여 준다. 판정은 Core 가 하고, 화면은 표시와 "자동 수정" 요청만 한다.
 */
import React from "react";
import {GroundedRepairPanel, RepairStage} from './GroundedRepairPanel';

export interface ReadinessIssue { code: string; severity: "error" | "warning"; message: string; fix: string; file?: string; auto_fix: boolean }
export interface BuildDiagnosis { code: string; title: string; cause: string; fix: string; lines: string[]; step?: string; repair?: {stage:RepairStage} }

const box: React.CSSProperties = { borderRadius: 5, padding: "8px 10px", marginBottom: 10, fontSize: 11, lineHeight: 1.6 };

export function sortIssues(issues: ReadinessIssue[]): ReadinessIssue[] {
  return [...issues].sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "error" ? -1 : 1));
}

export const ReadinessPanel: React.FC<{
  issues: ReadinessIssue[];
  onFix: (code: string) => void;
  onFixAll?: (codes: string[]) => void;
  fixing: string | null;
  notice?: string;
  title?: string;
}> = ({ issues, onFix, onFixAll, fixing, notice, title = "배포 준비 점검" }) => {
  if (!issues.length && !notice) return null;
  const errors = issues.filter(i => i.severity === "error").length;
  const fixable = sortIssues(issues).filter(i => i.auto_fix).map(i => i.code);
  return (
    <section aria-label={title} style={{ ...box, background: "#252526", border: `1px solid ${errors ? "#ef4444" : "#f59e0b"}` }}>
      <strong style={{ color: errors ? "#f87171" : "#f5b454" }}>
        {title}{issues.length ? ` · ${errors ? `빌드·실행 실패 예상 ${errors}건` : `확인 권장 ${issues.length}건`}` : ""}
      </strong>
      {notice && <div role="status" style={{ color: "#8fbf9f", marginTop: 4 }}>{notice}</div>}
      {onFixAll && fixable.length > 1 && (
        <button onClick={() => onFixAll(fixable)} disabled={fixing !== null}
          style={{ marginTop: 6, background: "#0e639c", color: "#fff", border: "none", borderRadius: 4, padding: "4px 10px", fontSize: 11, cursor: fixing ? "wait" : "pointer" }}>
          {fixing ? "수정 중…" : `자동 수정 가능한 ${fixable.length}건 모두 고치기`}
        </button>
      )}
      <ul style={{ listStyle: "none", padding: 0, margin: "6px 0 0" }}>
        {sortIssues(issues).map(issue => (
          <li key={issue.code} data-code={issue.code} style={{ borderTop: "1px solid #333", padding: "6px 0" }}>
            <div style={{ color: issue.severity === "error" ? "#f87171" : "#f5b454" }}>
              {issue.severity === "error" ? "✗ " : "! "}{issue.message}
            </div>
            <div style={{ color: "#bbb", marginTop: 2 }}>해결: {issue.fix}</div>
            {issue.auto_fix && (
              <button onClick={() => onFix(issue.code)} disabled={fixing !== null}
                style={{ marginTop: 4, background: "transparent", color: "#4a9eff", border: "1px solid #3f5f84", borderRadius: 4, padding: "2px 8px", fontSize: 11, cursor: fixing ? "wait" : "pointer" }}>
                {fixing === issue.code ? "수정 중…" : "자동 수정"}
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
};

export const BuildFailure: React.FC<{ diagnosis: BuildDiagnosis; raw?: string; restored?: string }> = ({ diagnosis, raw, restored }) => (
  <section role="alert" aria-label="빌드 실패 원인" style={{ ...box, background: "rgba(239,68,68,0.1)", border: "1px solid #ef4444", color: "#f3b1b1" }}>
    <strong style={{ color: "#ef4444", fontSize: 12 }}>배포 실패 — {diagnosis.title}</strong>
    <div style={{ marginTop: 4 }}>{diagnosis.cause}</div>
    <div style={{ marginTop: 4, color: "#e5e5e5" }}><b>해결:</b> {diagnosis.fix}</div>
    {diagnosis.step && <div style={{ marginTop: 4, color: "#aaa" }}>실패한 단계: <code>{diagnosis.step}</code></div>}
    {diagnosis.lines.length > 0 && (
      <pre style={{ margin: "6px 0 0", padding: "6px 8px", background: "#1e1e1e", borderRadius: 4, whiteSpace: "pre-wrap", overflowWrap: "anywhere", color: "#f0a0a0", fontFamily: "var(--vscode-editor-font-family, monospace)" }}>
        {diagnosis.lines.join("\n")}
      </pre>
    )}
    {restored && <div style={{ marginTop: 4, color: "#aaa" }}>{restored}</div>}
    {raw && (
      <details style={{ marginTop: 6 }}>
        <summary style={{ cursor: "pointer", color: "#aaa" }}>전체 빌드 출력 (마지막 부분)</summary>
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 240, overflow: "auto", color: "#ccc", fontFamily: "var(--vscode-editor-font-family, monospace)" }}>{raw}</pre>
      </details>
    )}
    <GroundedRepairPanel log={[diagnosis.cause,...diagnosis.lines,raw||''].join('\n')} stage={diagnosis.repair?.stage||'run'}/>
  </section>
);
