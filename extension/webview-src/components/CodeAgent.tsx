/**
 * ReCoder — 코드 작성 및 수정 패널 (Build 탭 하위)
 *  - 대상 폴더 지정 · 참고 파일 첨부 · 이어서 수정(멀티턴)
 *  - 파일별 적용 / 변경 보기(diff) / 시크릿 경고
 */
import React, { useCallback, useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

interface SecretWarning { rule: string; line: number; masked: string; }
interface CodeOp {
  action: "create" | "edit";
  file: string; language: string; content: string; rationale: string;
  secret_warnings?: SecretWarning[];
}
interface CodeResult { summary: string; ops: CodeOp[]; model: string; }
interface DecisionOption { key: string; label: string; summary: string; pros: string[]; cons: string[]; recommended: boolean; }
interface Decision { id: string; question: string; options: DecisionOption[]; impact: string; }
interface DecisionChoice { id: string; question: string; chosen_key: string; options: DecisionOption[]; }
interface Turn { id: number; prompt: string; status: "planning" | "generating" | "done" | "error"; result?: CodeResult; error?: string; }
interface CtxFile { path: string; content: string; }
interface PendingRequest { instruction: string; targetFolder: string; contextFiles: CtxFile[]; }
interface DecisionModal { requestId: number; decisions: Decision[]; selections: Record<string, string>; step: number; }

let _turnSeq = 1;

export const CodeAgent: React.FC<{ isActive: boolean }> = ({ isActive }) => {
  const { postMessage, useMessage } = useVSCodeApi();

  const [input, setInput] = useState("");
  const [targetFolder, setTargetFolder] = useState("");
  const [contextFiles, setContextFiles] = useState<CtxFile[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [applied, setApplied] = useState<Record<string, boolean>>({});
  const [decisionModal, setDecisionModal] = useState<DecisionModal | null>(null);
  const pendingRequestsRef = React.useRef<Record<number, PendingRequest>>({});

  useMessage(useCallback((msg) => {
    const { type, payload } = msg;
    if (type === "code.result") {
      const res = payload as CodeResult;
      setTurns((ts) => {
        const copy = [...ts];
        for (let i = copy.length - 1; i >= 0; i--) {
          if (copy[i].status === "generating") { copy[i] = { ...copy[i], status: "done", result: res }; break; }
        }
        return copy;
      });
    } else if (type === "code.error") {
      const m = (payload as { message?: string })?.message ?? String(payload);
      setTurns((ts) => {
        const copy = [...ts];
        const requestId = (payload as { requestId?: number })?.requestId;
        for (let i = copy.length - 1; i >= 0; i--) {
          if ((requestId === undefined || copy[i].id === requestId) && (copy[i].status === "planning" || copy[i].status === "generating")) {
            copy[i] = { ...copy[i], status: "error", error: m };
            break;
          }
        }
        return copy;
      });
    } else if (type === "code.planResult") {
      const plan = payload as { requestId?: number; decisions?: Decision[] };
      const requestId = plan.requestId;
      if (requestId === undefined) { return; }
      const request = pendingRequestsRef.current[requestId];
      if (!request) { return; }
      const decisions = plan.decisions ?? [];
      if (decisions.length === 0) {
        setTurns((ts) => ts.map((turn) => turn.id === requestId ? { ...turn, status: "generating" } : turn));
        postMessage("code.generate", { instruction: request.instruction, targetFolder: request.targetFolder, contextFiles: request.contextFiles, decisions: [] });
        return;
      }
      const selections: Record<string, string> = {};
      for (const decision of decisions) {
        selections[decision.id] = decision.options.find((option) => option.recommended)?.key ?? decision.options[0]?.key ?? "";
      }
      setDecisionModal({ requestId, decisions, selections, step: 0 });
    } else if (type === "code.folderPicked" || type === "code.setTargetFolder") {
      setTargetFolder((payload as { folder?: string })?.folder ?? "");
    } else if (type === "code.contextAdded") {
      const files = (payload as { files?: CtxFile[] })?.files ?? [];
      setContextFiles((cur) => {
        const seen = new Set(cur.map((c) => c.path));
        return [...cur, ...files.filter((f) => !seen.has(f.path))];
      });
    }
  }, []));

  const send = useCallback(() => {
    const text = input.trim();
    if (!text) { return; }
    const id = _turnSeq++;
    pendingRequestsRef.current[id] = { instruction: text, targetFolder, contextFiles };
    setTurns((ts) => [...ts, { id, prompt: text, status: "planning" }]);
    postMessage("code.plan", { requestId: id, instruction: text, targetFolder, contextFiles });
    setInput("");
  }, [input, targetFolder, contextFiles, postMessage]);

  const chooseDecision = useCallback((key: string) => {
    setDecisionModal((current) => current ? {
      ...current,
      selections: { ...current.selections, [current.decisions[current.step].id]: key },
    } : current);
  }, []);

  const cancelDecision = useCallback(() => {
    if (!decisionModal) { return; }
    setTurns((ts) => ts.map((turn) => turn.id === decisionModal.requestId
      ? { ...turn, status: "error", error: "설계 결정을 취소해서 생성을 중단했습니다." }
      : turn));
    delete pendingRequestsRef.current[decisionModal.requestId];
    setDecisionModal(null);
  }, [decisionModal]);

  const confirmDecisions = useCallback(() => {
    if (!decisionModal) { return; }
    const request = pendingRequestsRef.current[decisionModal.requestId];
    if (!request) { setDecisionModal(null); return; }
    const choices: DecisionChoice[] = decisionModal.decisions.map((decision) => ({
      id: decision.id,
      question: decision.question,
      chosen_key: decisionModal.selections[decision.id],
      options: decision.options,
    }));
    setTurns((ts) => ts.map((turn) => turn.id === decisionModal.requestId ? { ...turn, status: "generating" } : turn));
    setDecisionModal(null);
    postMessage("code.generate", { instruction: request.instruction, targetFolder: request.targetFolder, contextFiles: request.contextFiles, decisions: choices });
  }, [decisionModal, postMessage]);

  const applyOp = useCallback((turnId: number, op: CodeOp) => {
    postMessage("code.apply", { file: op.file, content: op.content, targetFolder });
    setApplied((a) => ({ ...a, [`${turnId}:${op.file}`]: true }));
  }, [postMessage, targetFolder]);

  const applyAll = useCallback((turn: Turn) => {
    if (!turn.result) { return; }
    postMessage("code.applyAll", { ops: turn.result.ops, targetFolder });
    setApplied((a) => {
      const copy = { ...a };
      for (const op of turn.result!.ops) { copy[`${turn.id}:${op.file}`] = true; }
      return copy;
    });
  }, [postMessage, targetFolder]);

  const showDiff = useCallback((op: CodeOp) => {
    postMessage("code.diff", { file: op.file, content: op.content, targetFolder });
  }, [postMessage, targetFolder]);

  const label: React.CSSProperties = {
    fontSize: 12, fontWeight: 600, color: "var(--vscode-foreground, #ddd)", marginBottom: 8,
  };
  const linkBtn: React.CSSProperties = {
    fontSize: 11, border: "none", background: "transparent",
    color: "var(--vscode-textLink-foreground, #3794ff)", padding: 0, cursor: "pointer",
  };
  const primaryBtn: React.CSSProperties = {
    background: "var(--vscode-button-background, #2563eb)", color: "var(--vscode-button-foreground, #fff)",
    border: "none", borderRadius: 4, padding: "5px 12px", fontSize: 12, fontWeight: 500, cursor: "pointer",
  };
  const ghostBtn: React.CSSProperties = {
    background: "transparent", color: "var(--vscode-foreground, #ccc)",
    border: "1px solid var(--vscode-input-border, #3f3f3f)", borderRadius: 4, padding: "3px 9px",
    fontSize: 11, cursor: "pointer",
  };
  const isBusy = turns.some((turn) => turn.status === "planning" || turn.status === "generating");

  return (
    <div style={{ borderTop: "1px solid var(--vscode-panel-border, #333)", margin: "16px 0 0", paddingTop: 14 }}>
      <style>{`
        .rc-cg-input { transition: border-color .12s ease, box-shadow .12s ease; }
        .rc-cg-input:focus { border-color: var(--vscode-focusBorder, #3794ff) !important; box-shadow: 0 0 0 1px var(--vscode-focusBorder, #3794ff); }
        .rc-cg-input::placeholder { color: var(--vscode-input-placeholderForeground, #6b6b6b); }
        .rc-cg-send { transition: filter .12s ease, transform .05s ease; }
        .rc-cg-send:hover:not(:disabled) { filter: brightness(1.12); }
        .rc-cg-send:active:not(:disabled) { transform: translateY(1px); }
        .rc-decision-option:hover { border-color: var(--vscode-focusBorder, #3794ff) !important; }
      `}</style>
      {decisionModal && (() => {
        const decision = decisionModal.decisions[decisionModal.step];
        const isLast = decisionModal.step === decisionModal.decisions.length - 1;
        return (
          <div role="dialog" aria-modal="true" aria-label="설계 결정" style={{ position: "fixed", inset: 0, zIndex: 1000, display: "grid", placeItems: "center", padding: 18, background: "rgba(0,0,0,.58)", backdropFilter: "blur(2px)" }}>
            <div style={{ width: "min(560px, 100%)", maxHeight: "calc(100vh - 36px)", overflowY: "auto", border: "1px solid var(--vscode-widget-border, #454545)", borderRadius: 10, background: "var(--vscode-editorWidget-background, #252526)", boxShadow: "0 18px 48px rgba(0,0,0,.45)" }}>
              <div style={{ padding: "15px 18px 12px", borderBottom: "1px solid var(--vscode-panel-border, #3b3b3b)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ width: 4, height: 20, borderRadius: 2, background: "var(--vscode-textLink-foreground, #3794ff)" }} />
                  <strong style={{ fontSize: 15 }}>설계 결정을 골라주세요</strong>
                  <span style={{ marginLeft: "auto", borderRadius: 99, padding: "3px 8px", background: "var(--vscode-badge-background, #4d4d4d)", color: "var(--vscode-badge-foreground, #fff)", fontSize: 11, fontWeight: 600 }}>설계 결정 {decisionModal.step + 1}/{decisionModal.decisions.length}</span>
                </div>
                <div style={{ marginTop: 9, color: "var(--vscode-descriptionForeground, #aaa)", fontSize: 11.5, lineHeight: 1.5 }}>코드 생성 전에 프로젝트 구조에 영향을 주는 선택을 확인합니다.</div>
              </div>
              <div style={{ padding: "18px" }}>
                <h3 style={{ margin: 0, color: "var(--vscode-foreground, #eee)", fontSize: 18, lineHeight: 1.4 }}>{decision.question}</h3>
                {decision.impact && <p style={{ margin: "7px 0 16px", color: "var(--vscode-descriptionForeground, #aaa)", fontSize: 12, lineHeight: 1.5 }}>{decision.impact}</p>}
                <div style={{ display: "grid", gap: 9 }}>
                  {decision.options.map((option) => {
                    const selected = decisionModal.selections[decision.id] === option.key;
                    return (
                      <label key={option.key} className="rc-decision-option" style={{ display: "flex", gap: 11, alignItems: "flex-start", cursor: "pointer", padding: "12px 13px", border: `1px solid ${selected ? "var(--vscode-focusBorder, #3794ff)" : "var(--vscode-panel-border, #3f3f3f)"}`, borderRadius: 8, background: selected ? "var(--vscode-list-activeSelectionBackground, rgba(55,148,255,.18))" : "var(--vscode-editor-background, #1e1e1e)" }}>
                        <input type="radio" name={`decision-${decision.id}`} checked={selected} onChange={() => chooseDecision(option.key)} style={{ marginTop: 3, accentColor: "var(--vscode-focusBorder, #3794ff)" }} />
                        <span style={{ minWidth: 0 }}>
                          <span style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 650, color: "var(--vscode-foreground, #eee)" }}>
                            {option.label}
                            {option.recommended && <span style={{ borderRadius: 99, padding: "2px 6px", background: "rgba(81, 188, 120, .18)", color: "#78d89b", fontSize: 10, fontWeight: 700 }}>추천</span>}
                          </span>
                          {option.summary && <span style={{ display: "block", marginTop: 4, color: "var(--vscode-descriptionForeground, #aaa)", fontSize: 11.5, lineHeight: 1.45 }}>{option.summary}</span>}
                          {(option.pros?.length > 0 || option.cons?.length > 0) && <span style={{ display: "block", marginTop: 6, color: "var(--vscode-descriptionForeground, #888)", fontSize: 10.5, lineHeight: 1.5 }}>{[...(option.pros ?? []).map((text) => `+ ${text}`), ...(option.cons ?? []).map((text) => `− ${text}`)].join("  ·  ")}</span>}
                        </span>
                      </label>
                    );
                  })}
                </div>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 18px 16px", borderTop: "1px solid var(--vscode-panel-border, #3b3b3b)" }}>
                <button onClick={cancelDecision} style={{ ...ghostBtn, padding: "7px 11px" }}>취소</button>
                {decisionModal.step > 0 && <button onClick={() => setDecisionModal((current) => current ? { ...current, step: current.step - 1 } : current)} style={{ ...ghostBtn, padding: "7px 11px" }}>이전</button>}
                <button onClick={() => isLast ? confirmDecisions() : setDecisionModal((current) => current ? { ...current, step: current.step + 1 } : current)} disabled={!decisionModal.selections[decision.id]} style={{ ...primaryBtn, marginLeft: "auto", padding: "8px 13px", opacity: decisionModal.selections[decision.id] ? 1 : .5 }}>
                  {isLast ? "이 선택으로 생성 →" : "다음 결정 →"}
                </button>
              </div>
            </div>
          </div>
        );
      })()}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <span style={{ width: 3, height: 14, borderRadius: 2, background: "var(--vscode-textLink-foreground, #3794ff)" }} />
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--vscode-foreground, #eee)", letterSpacing: 0.2 }}>코드 작성 및 수정</span>
        <span style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #888)", marginLeft: "auto", border: "1px solid var(--vscode-panel-border, #3f3f3f)", borderRadius: 10, padding: "1px 8px" }}>AI 코드 생성</span>
      </div>
      <div style={{ fontSize: 11, color: "var(--vscode-descriptionForeground, #999)", marginBottom: 10, lineHeight: 1.5 }}>
        자연어로 새 코드를 만들거나 기존 코드를 고칩니다. 생성 결과는 파일별로 확인 후 적용됩니다.
      </div>
      {!isActive && (
        <div style={{ marginBottom: 10, border: "1px solid var(--vscode-inputValidation-warningBorder, #cca700)", background: "var(--vscode-inputValidation-warningBackground, rgba(204,167,0,.12))", borderRadius: 5, padding: "7px 9px", color: "var(--vscode-editorWarning-foreground, #cca700)", fontSize: 11, lineHeight: 1.45 }}>
          AI 연결이 아직 준비되지 않았습니다. 요청은 입력할 수 있지만, 실제 처리는 Core/AI 연결 후에 가능합니다.
        </div>
      )}

      {/* 대상 폴더 · 참고 파일 */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: contextFiles.length ? 6 : 8, fontSize: 11, color: "var(--vscode-descriptionForeground, #999)" }}>
        <span>
          위치{" "}
          <button onClick={() => postMessage("code.pickFolder")} style={linkBtn}>{targetFolder || "루트"}</button>
          {targetFolder && (
            <button onClick={() => setTargetFolder("")} style={{ ...linkBtn, marginLeft: 6, opacity: 0.7 }}>지우기</button>
          )}
        </span>
        <button onClick={() => postMessage("code.pickContext")} style={linkBtn}>참고 파일 추가</button>
      </div>
      {contextFiles.length > 0 && (
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginBottom: 8 }}>
          {contextFiles.map((c) => (
            <span key={c.path} style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 10, background: "var(--vscode-badge-background, #2a2d2e)", color: "var(--vscode-badge-foreground, #ccc)", borderRadius: 4, padding: "2px 7px" }}>
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 150 }}>{c.path}</span>
              <span onClick={() => setContextFiles((cur) => cur.filter((x) => x.path !== c.path))} style={{ cursor: "pointer", opacity: 0.6 }}>×</span>
            </span>
          ))}
        </div>
      )}

      {/* 히스토리 */}
      {turns.map((turn) => (
        <div key={turn.id} style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 11, color: "var(--vscode-descriptionForeground, #888)", marginBottom: 6, paddingLeft: 8, borderLeft: "2px solid var(--vscode-panel-border, #3f3f3f)" }}>
            {turn.prompt}
          </div>

          {(turn.status === "planning" || turn.status === "generating") && (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--vscode-descriptionForeground, #888)", fontSize: 11, padding: "2px 0 6px" }}>
                <div style={{ width: 11, height: 11, border: "2px solid #3f3f3f", borderTopColor: "var(--vscode-progressBar-background, #3794ff)", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} />
                <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
                {turn.status === "planning" ? "설계 결정을 준비하는 중…" : "코드 생성 중…"}
              </div>
            </>
          )}
          {turn.status === "error" && (
            <div style={{ background: "var(--vscode-inputValidation-errorBackground, rgba(239,68,68,0.1))", border: "1px solid var(--vscode-inputValidation-errorBorder, #ef4444)", borderRadius: 4, padding: "7px 10px", color: "var(--vscode-errorForeground, #f48771)", fontSize: 11 }}>{turn.error}</div>
          )}
          {turn.status === "done" && turn.result && (
            <div>
              {turn.result.ops.length > 1 && (
                <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 6 }}>
                  <button onClick={() => applyAll(turn)} style={primaryBtn}>모두 적용</button>
                </div>
              )}
              {turn.result.ops.map((op, i) => {
                const key = `${turn.id}:${op.file}`;
                const warned = !!(op.secret_warnings && op.secret_warnings.length);
                return (
                  <div key={i} style={{ marginBottom: 7, border: "1px solid var(--vscode-panel-border, #333)", borderRadius: 4, overflow: "hidden" }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 6, background: "var(--vscode-editorGroupHeader-tabsBackground, #2d2d2d)", padding: "5px 8px" }}>
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 6, minWidth: 0 }}>
                        <span style={{ fontSize: 9, fontWeight: 600, color: op.action === "create" ? "#6cc070" : "#d6a55c" }}>
                          {op.action === "create" ? "새 파일" : "수정"}
                        </span>
                        <span style={{ fontSize: 11.5, fontFamily: "var(--vscode-editor-font-family, monospace)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                          {targetFolder ? `${targetFolder}/${op.file}` : op.file}
                        </span>
                      </span>
                      <span style={{ display: "inline-flex", gap: 6, flexShrink: 0 }}>
                        {op.action === "edit" && <button onClick={() => showDiff(op)} style={ghostBtn}>변경 보기</button>}
                        <button onClick={() => applyOp(turn.id, op)} disabled={applied[key]}
                          style={{ ...primaryBtn, padding: "3px 11px", fontSize: 11, ...(applied[key] ? { background: "transparent", color: "#6cc070", cursor: "default" } : {}) }}>
                          {applied[key] ? "적용됨" : "적용"}
                        </button>
                      </span>
                    </div>
                    <pre style={{ margin: 0, background: "var(--vscode-textCodeBlock-background, #1e1e1e)", color: "var(--vscode-editor-foreground, #ddd)", padding: "6px 8px", fontFamily: "var(--vscode-editor-font-family, monospace)", fontSize: 10.5, maxHeight: 150, overflow: "auto", whiteSpace: "pre", lineHeight: 1.5 }}>
                      {op.content.length > 1000 ? op.content.slice(0, 1000) + "\n…" : op.content}
                    </pre>
                    {warned && (
                      <div style={{ background: "rgba(216,165,92,0.12)", borderTop: "1px solid rgba(216,165,92,0.3)", padding: "5px 8px", fontSize: 10.5, color: "#d6a55c" }}>
                        키가 코드에 포함된 것 같습니다 ({op.secret_warnings!.length}건). .env로 옮기세요.
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ))}

      {/* 입력 */}
      <textarea
        className="rc-cg-input"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { send(); } }}
        placeholder={turns.length ? "이어서 수정 요청 (예: 버튼 색을 파랑으로)" : "만들거나 고칠 내용을 입력 (예: SQLite 게시판 REST API를 FastAPI로 만들어줘)"}
        disabled={isBusy}
        style={{ width: "100%", boxSizing: "border-box", minHeight: 130, background: "var(--vscode-input-background, #252526)", color: "var(--vscode-input-foreground, #ccc)", border: "1px solid var(--vscode-input-border, #3f3f3f)", borderRadius: 6, padding: "10px 12px", fontSize: 12.5, fontFamily: "var(--vscode-font-family, sans-serif)", resize: "vertical", outline: "none", lineHeight: 1.6, opacity: isBusy ? .6 : 1 }}
      />
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 8 }}>
        <button onClick={send} disabled={!input.trim() || isBusy} className="rc-cg-send" style={{ ...primaryBtn, padding: "7px 16px", borderRadius: 6, opacity: input.trim() && !isBusy ? 1 : 0.5, cursor: input.trim() && !isBusy ? "pointer" : "not-allowed" }}>
          보내기
        </button>
        <span style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #777)" }}>Ctrl+Enter 로 전송</span>
      </div>
    </div>
  );
};

export default CodeAgent;
