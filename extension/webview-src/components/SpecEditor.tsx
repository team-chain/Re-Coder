/**
 * 명세 · 합격 조건 (2.0.43) — 결정 창의 마지막 단계.
 *
 * 읽기가 먼저다: 조건은 문장 목록으로 보이고, 고칠 줄만 눌러서 편집한다(모든 줄이 입력칸이면 "검토"가 아니라 "양식 작성"이 된다).
 * 명세 값은 부모(CodeAgent 결정 창)가 들고, 여기서는 지금 편집 중인 줄만 기억한다.
 */
import React, { useState } from "react";
import { Icon } from "./ui/icons";
import { Checkbox } from "./Check";
import { EditableSpec, KIND_LABEL, SPEC_KINDS, SpecIssue, SpecKind, addCriterion, removeCriterion, updateCriterion } from "./specModel";

const C = {
  fg: "var(--vscode-foreground, #e6e6e6)",
  dim: "var(--vscode-descriptionForeground, #9a9a9a)",
  faint: "var(--vscode-disabledForeground, #6f6f6f)",
  line: "var(--vscode-widget-border, var(--vscode-panel-border, #3c3c3c))",
  inputBg: "var(--vscode-input-background, #1f1f1f)",
  inputFg: "var(--vscode-input-foreground, #ddd)",
  link: "var(--vscode-textLink-foreground, #4daafc)",
  warn: "var(--vscode-editorWarning-foreground, #cca700)",
  err: "var(--vscode-errorForeground, #f48771)",
  mono: "var(--vscode-editor-font-family, monospace)",
};

const CSS = `
.rc-spec { display: grid; gap: 18px; }
.rc-spec-sec { font-size: 11px; font-weight: 650; letter-spacing: .3px; color: ${C.dim}; margin: 0 0 6px; display: flex; align-items: baseline; gap: 8px; }
.rc-spec-sec small { font-weight: 400; letter-spacing: 0; color: ${C.faint}; }
.rc-spec-icon { border: 0; background: transparent; color: ${C.faint}; cursor: pointer; padding: 2px 5px; border-radius: 4px; font-size: 12px; line-height: 1; opacity: 0; transition: opacity .12s; }
.rc-spec-row:hover .rc-spec-icon, .rc-spec-head:hover .rc-spec-icon, .rc-spec-icon:focus-visible { opacity: 1; }
.rc-spec-icon:hover { color: ${C.fg}; background: var(--vscode-toolbar-hoverBackground, rgba(90,93,94,.31)); }
.rc-spec-row { display: grid; grid-template-columns: 18px 38px 1fr auto; gap: 0 8px; align-items: start; padding: 9px 4px; border-top: 1px solid ${C.line}; }
.rc-spec-row:first-child { border-top: 0; }
.rc-spec-row.off .rc-spec-text { color: ${C.faint}; text-decoration: line-through; }
.rc-spec-text { font-size: 13px; line-height: 1.5; color: ${C.fg}; cursor: text; overflow-wrap: anywhere; }
.rc-spec-check { margin-top: 2px; font-size: 11.5px; color: ${C.dim}; font-family: ${C.mono}; overflow-wrap: anywhere; }
.rc-spec-tag { display: inline-block; margin-right: 6px; padding: 0 5px; border-radius: 3px; font-size: 10.5px; line-height: 16px; vertical-align: 1px; color: ${C.dim}; background: var(--vscode-badge-background, rgba(128,128,128,.18)); }
.rc-spec-chip { display: inline-block; margin: 0 6px 6px 0; padding: 2px 9px; border-radius: 99px; font-size: 12px; border: 1px solid ${C.line}; color: ${C.fg}; }
.rc-spec-chip.out { color: ${C.dim}; border-style: dashed; }
.rc-spec-field { width: 100%; box-sizing: border-box; background: ${C.inputBg}; color: ${C.inputFg}; border: 1px solid ${C.line}; border-radius: 5px; padding: 6px 8px; font: inherit; font-size: 12.5px; line-height: 1.5; }
.rc-spec-field:focus { outline: none; border-color: var(--vscode-focusBorder, #3794ff); }
.rc-spec-add { white-space: nowrap; border: 0; background: transparent; color: ${C.link}; cursor: pointer; padding: 6px 4px; font-size: 12px; }
`;

export interface SpecEditorProps {
  spec: EditableSpec;
  issues: SpecIssue[];
  onChange: (next: EditableSpec) => void;
  disabled?: boolean;
}

const lines = (text: string) => text.split(/\r?\n/).map((s) => s.replace(/^\s*[-*•]\s+/, "").trim()).filter(Boolean);

export const SpecEditor: React.FC<SpecEditorProps> = ({ spec, issues, onChange, disabled }) => {
  //: 지금 고치는 줄(조건 키) · "goal" · "scope". 내용이 빈 조건은 늘 편집 상태로 연다(어디에 쓰는지 보이게).
  const [editing, setEditing] = useState<string | null>(null);
  const rowIssue = (key: string) => issues.find((i) => i.key === key);
  const general = issues.filter((i) => !i.key);
  const included = spec.criteria.filter((c) => c.included).length;
  const fromAi = spec.criteria.some((c) => c.source === "ai");
  const inList = lines(spec.inScope), outList = lines(spec.outOfScope);
  //: 번호는 기록 파일과 같게 — 코어는 포함한(내용 있는) 조건에만 AC-1, AC-2… 를 매긴다. 뺀 줄은 번호 없이 "—".
  const acNo = new Map<string, number>();
  for (const c of spec.criteria) if (c.included && c.text.trim()) acNo.set(c.key, acNo.size + 1);

  return (
    <div className="rc-spec" data-testid="spec-editor">
      <style>{CSS}</style>

      {/* 목표 */}
      <div className="rc-spec-head">
        <div className="rc-spec-sec">목표</div>
        {editing === "goal" ? (
          <input className="rc-spec-field" autoFocus value={spec.goal} maxLength={200} disabled={disabled} aria-label="목표"
            onChange={(e) => onChange({ ...spec, goal: e.target.value })} onBlur={() => setEditing(null)}
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === "Escape") setEditing(null); }} />
        ) : (
          <div style={{ display: "flex", alignItems: "flex-start", gap: 6 }}>
            <div onClick={() => !disabled && setEditing("goal")} style={{ fontSize: 15, fontWeight: 600, lineHeight: 1.45, color: spec.goal ? C.fg : C.faint, cursor: "text" }}>
              {spec.goal || "끝나면 무엇을 할 수 있게 되는지 한 문장으로 적어 주세요"}
            </div>
            <button type="button" className="rc-spec-icon" aria-label="목표 고치기" disabled={disabled} onClick={() => setEditing("goal")}><Icon name="edit" size={14} /></button>
          </div>
        )}
      </div>

      {/* 범위 */}
      <div className="rc-spec-head">
        <div className="rc-spec-sec">범위
          <button type="button" className="rc-spec-icon" aria-label="범위 고치기" disabled={disabled} onClick={() => setEditing(editing === "scope" ? null : "scope")}><Icon name="edit" size={14} /></button>
        </div>
        {editing === "scope" ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 8 }}>
            <label style={{ fontSize: 11, color: C.dim }}>만들 것 · 한 줄에 하나
              <textarea className="rc-spec-field" rows={3} value={spec.inScope} disabled={disabled} style={{ marginTop: 4, resize: "vertical" }}
                onChange={(e) => onChange({ ...spec, inScope: e.target.value })} />
            </label>
            <label style={{ fontSize: 11, color: C.dim }}>이번에 만들지 않음
              <textarea className="rc-spec-field" rows={3} value={spec.outOfScope} disabled={disabled} style={{ marginTop: 4, resize: "vertical" }}
                onChange={(e) => onChange({ ...spec, outOfScope: e.target.value })} />
            </label>
            <div><button type="button" className="rc-spec-add" style={{ paddingLeft: 0 }} onClick={() => setEditing(null)}>완료</button></div>
          </div>
        ) : (
          <div style={{ fontSize: 12 }}>
            <div>{inList.length ? inList.map((s, i) => <span key={i} className="rc-spec-chip">{s}</span>) : <span style={{ color: C.faint }}>적지 않음</span>}</div>
            {outList.length > 0 && (
              <div style={{ marginTop: 2 }}>
                <span style={{ color: C.faint, marginRight: 8 }}>이번에 만들지 않음</span>
                {outList.map((s, i) => <span key={i} className="rc-spec-chip out">{s}</span>)}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 합격 조건 */}
      <div>
        <div className="rc-spec-sec">합격 조건 <small>{included}개 · 체크를 끄면 이번 생성에서 뺍니다{fromAi ? " · AI 초안 — 문장을 누르면 고칠 수 있어요" : ""}</small></div>
        <div style={{ border: `1px solid ${C.line}`, borderRadius: 8, padding: "0 10px" }}>
          {spec.criteria.map((c, i) => {
            const issue = rowIssue(c.key);
            const open = !disabled && (editing === c.key || !c.text.trim());
            return (
              <div key={c.key} data-spec-row={c.key} className={`rc-spec-row${c.included ? "" : " off"}`}>
                <div style={{ paddingTop: 2 }}>
                  <Checkbox compact inline checked={c.included} disabled={disabled} ariaLabel={`합격 조건 ${i + 1} 포함`}
                    onChange={(checked) => onChange(updateCriterion(spec, c.key, { included: checked }))} />
                </div>
                <div style={{ fontSize: 11, fontWeight: 650, color: C.faint, paddingTop: 2 }} title={acNo.has(c.key) ? undefined : "이번 생성에서 뺀 조건"}>{acNo.has(c.key) ? `AC-${acNo.get(c.key)}` : "—"}</div>
                {open ? (
                  <div style={{ display: "grid", gap: 5 }}>
                    <textarea className="rc-spec-field" rows={2} autoFocus={editing === c.key} value={c.text} maxLength={240}
                      placeholder="누가 무엇을 하면 어떤 결과가 나오나요 (예: 수량 0 으로 담으면 400 과 안내 문구)" aria-label={`합격 조건 ${i + 1} 내용`}
                      onChange={(e) => onChange(updateCriterion(spec, c.key, { text: e.target.value }))} style={{ resize: "vertical" }} />
                    <div style={{ display: "flex", gap: 6 }}>
                      <select className="rc-spec-field" value={c.kind} aria-label={`합격 조건 ${i + 1} 종류`} style={{ width: "auto", padding: "4px 6px" }}
                        onChange={(e) => onChange(updateCriterion(spec, c.key, { kind: e.target.value as SpecKind }))}>
                        {SPEC_KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
                      </select>
                      <input className="rc-spec-field" value={c.check} maxLength={200} placeholder="확인 방법 (예: POST /api/cart qty=0 → 400)"
                        aria-label={`합격 조건 ${i + 1} 확인 방법`} style={{ fontFamily: C.mono, fontSize: 11.5 }}
                        onChange={(e) => onChange(updateCriterion(spec, c.key, { check: e.target.value }))}
                        onKeyDown={(e) => { if (e.key === "Enter") setEditing(null); }} />
                      <button type="button" className="rc-spec-add" onClick={() => setEditing(null)}>완료</button>
                    </div>
                  </div>
                ) : (
                  <div onClick={() => !disabled && setEditing(c.key)}>
                    <div className="rc-spec-text"><span className="rc-spec-tag">{KIND_LABEL[c.kind]}</span>{c.text}</div>
                    {c.check
                      ? <div className="rc-spec-check">확인 · {c.check}</div>
                      : <div className="rc-spec-check" style={{ color: C.warn, fontFamily: "inherit" }}>확인 방법 없음</div>}
                  </div>
                )}
                <div style={{ display: "flex", gap: 2 }}>
                  {!open && <button type="button" className="rc-spec-icon" aria-label={`합격 조건 ${i + 1} 고치기`} disabled={disabled} onClick={() => setEditing(c.key)}><Icon name="edit" size={14} /></button>}
                  <button type="button" className="rc-spec-icon" aria-label={`합격 조건 ${i + 1} 지우기`} disabled={disabled}
                    onClick={() => { if (editing === c.key) setEditing(null); onChange(removeCriterion(spec, c.key)); }}>×</button>
                </div>
                {issue && c.included && c.text.trim() && issue.message !== "확인 방법이 비어 있습니다." && (
                  <div role="note" style={{ gridColumn: "3 / 5", marginTop: 4, fontSize: 11, color: issue.level === "block" ? C.err : C.warn }}>{issue.message}</div>
                )}
              </div>
            );
          })}
        </div>
        <button type="button" className="rc-spec-add" disabled={disabled}
          onClick={() => { const next = addCriterion(spec); onChange(next); setEditing(next.criteria[next.criteria.length - 1]?.key ?? null); }}>
          + 합격 조건 추가
        </button>
      </div>

      {general.length > 0 && (
        <div data-testid="spec-issues" style={{ display: "grid", gap: 3, padding: "8px 10px", borderRadius: 6, background: "rgba(204,167,0,.08)" }}>
          {general.map((i, n) => (
            <div key={n} role={i.level === "block" ? "alert" : "note"} style={{ fontSize: 11.5, lineHeight: 1.5, color: i.level === "block" ? C.err : C.warn }}>{i.message}</div>
          ))}
        </div>
      )}
    </div>
  );
};

export default SpecEditor;
