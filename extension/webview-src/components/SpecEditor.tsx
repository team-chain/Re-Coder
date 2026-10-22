/**
 * 명세 · 합격 조건 편집 (2.0.38) — 결정 창의 마지막 단계.
 * 상태는 부모(CodeAgent 의 결정 창)가 들고, 여기서는 그리기와 고친 값 돌려주기만 한다(훅 없음).
 */
import React from "react";
import { Checkbox } from "./Check";
import { EditableSpec, KIND_LABEL, SPEC_KINDS, SpecIssue, SpecKind, addCriterion, removeCriterion, updateCriterion } from "./specModel";

const C = {
  fg: "var(--vscode-foreground, #eee)",
  dim: "var(--vscode-descriptionForeground, #aaa)",
  line: "var(--vscode-widget-border, var(--vscode-panel-border, #454545))",
  inputBg: "var(--vscode-input-background, #1f1f1f)",
  inputFg: "var(--vscode-input-foreground, #ddd)",
  link: "var(--vscode-textLink-foreground, #4daafc)",
  warn: "var(--vscode-editorWarning-foreground, #cca700)",
  err: "var(--vscode-errorForeground, #f48771)",
};

const field: React.CSSProperties = {
  width: "100%", boxSizing: "border-box", background: C.inputBg, color: C.inputFg,
  border: `1px solid ${C.line}`, borderRadius: 5, padding: "6px 8px", fontSize: 12.5, lineHeight: 1.5,
  fontFamily: "var(--vscode-font-family, sans-serif)",
};
const label: React.CSSProperties = { display: "block", fontSize: 11, fontWeight: 650, color: C.dim, margin: "0 0 4px" };

export interface SpecEditorProps {
  spec: EditableSpec;
  issues: SpecIssue[];
  onChange: (next: EditableSpec) => void;
  disabled?: boolean;
}

export const SpecEditor: React.FC<SpecEditorProps> = ({ spec, issues, onChange, disabled }) => {
  const rowIssue = (key: string) => issues.find((i) => i.key === key);
  const general = issues.filter((i) => !i.key);
  const included = spec.criteria.filter((c) => c.included).length;
  return (
    <div data-testid="spec-editor" style={{ display: "grid", gap: 14 }}>
      <div>
        <label style={label} htmlFor="rc-spec-goal">목표 — 끝나면 무엇을 할 수 있게 되나요</label>
        <input id="rc-spec-goal" style={field} value={spec.goal} disabled={disabled} maxLength={200}
          onChange={(e) => onChange({ ...spec, goal: e.target.value })} />
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10 }}>
        <div>
          <label style={label} htmlFor="rc-spec-in">만들 범위 (한 줄에 하나)</label>
          <textarea id="rc-spec-in" rows={3} style={{ ...field, resize: "vertical" }} value={spec.inScope} disabled={disabled}
            onChange={(e) => onChange({ ...spec, inScope: e.target.value })} />
        </div>
        <div>
          <label style={label} htmlFor="rc-spec-out">이번에 만들지 않음</label>
          <textarea id="rc-spec-out" rows={3} style={{ ...field, resize: "vertical" }} value={spec.outOfScope} disabled={disabled}
            onChange={(e) => onChange({ ...spec, outOfScope: e.target.value })} />
        </div>
      </div>

      <div>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 6 }}>
          <span style={{ ...label, margin: 0, color: C.fg, fontSize: 12.5 }}>합격 조건</span>
          <span style={{ fontSize: 11, color: C.dim }}>{included}개 포함 · 체크를 끄면 이번 생성에서 뺍니다</span>
        </div>
        <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
          {spec.criteria.map((c, i) => {
            const issue = rowIssue(c.key);
            return (
              <li key={c.key} data-spec-row={c.key} style={{ border: `1px solid ${C.line}`, borderRadius: 7, padding: "8px 9px", opacity: c.included ? 1 : .55 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                  <Checkbox compact inline checked={c.included} disabled={disabled} ariaLabel={`합격 조건 ${i + 1} 포함`}
                    onChange={(checked) => onChange(updateCriterion(spec, c.key, { included: checked }))} />
                  <span style={{ fontSize: 11, fontWeight: 700, color: C.dim, minWidth: 34 }}>AC-{i + 1}</span>
                  <select value={c.kind} disabled={disabled} aria-label={`합격 조건 ${i + 1} 종류`}
                    onChange={(e) => onChange(updateCriterion(spec, c.key, { kind: e.target.value as SpecKind }))}
                    style={{ ...field, width: "auto", padding: "2px 6px", fontSize: 11.5 }}>
                    {SPEC_KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
                  </select>
                  {c.source === "ai" && <span title="AI 가 쓴 초안 — 고치면 직접 쓴 조건이 됩니다" style={{ fontSize: 10, color: C.dim, border: `1px solid ${C.line}`, borderRadius: 99, padding: "0 6px" }}>AI 초안</span>}
                  <button type="button" aria-label={`합격 조건 ${i + 1} 지우기`} disabled={disabled}
                    onClick={() => onChange(removeCriterion(spec, c.key))}
                    style={{ marginLeft: "auto", border: 0, background: "transparent", color: C.dim, cursor: "pointer", fontSize: 14, lineHeight: 1 }}>×</button>
                </div>
                <textarea rows={2} value={c.text} disabled={disabled} placeholder="누가 무엇을 하면 어떤 결과가 나오는지 (예: 수량 0 으로 담으면 400 과 안내 문구)"
                  aria-label={`합격 조건 ${i + 1} 내용`} maxLength={240}
                  onChange={(e) => onChange(updateCriterion(spec, c.key, { text: e.target.value }))}
                  style={{ ...field, marginTop: 6, resize: "vertical" }} />
                <input value={c.check} disabled={disabled} placeholder="확인 방법 (예: POST /api/cart qty=0 → 400)" maxLength={200}
                  aria-label={`합격 조건 ${i + 1} 확인 방법`}
                  onChange={(e) => onChange(updateCriterion(spec, c.key, { check: e.target.value }))}
                  style={{ ...field, marginTop: 5, fontSize: 11.5, fontFamily: "var(--vscode-editor-font-family, monospace)" }} />
                {issue && <div role="note" style={{ marginTop: 5, fontSize: 11, color: issue.level === "block" ? C.err : C.warn }}>{issue.message}</div>}
              </li>
            );
          })}
        </ol>
        <button type="button" disabled={disabled} onClick={() => onChange(addCriterion(spec))}
          style={{ marginTop: 8, border: `1px dashed ${C.line}`, background: "transparent", color: C.link, borderRadius: 6, padding: "5px 10px", fontSize: 12, cursor: "pointer" }}>
          + 합격 조건 추가
        </button>
      </div>

      {general.length > 0 && (
        <div data-testid="spec-issues" style={{ display: "grid", gap: 4 }}>
          {general.map((i, n) => (
            <div key={n} role={i.level === "block" ? "alert" : "note"} style={{ fontSize: 11.5, lineHeight: 1.5, color: i.level === "block" ? C.err : C.warn }}>· {i.message}</div>
          ))}
        </div>
      )}
    </div>
  );
};

export default SpecEditor;
