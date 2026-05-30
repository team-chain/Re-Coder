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
interface Turn { id: number; prompt: string; status: "loading" | "done" | "error"; result?: CodeResult; error?: string; }
interface CtxFile { path: string; content: string; }

let _turnSeq = 1;

export const CodeAgent: React.FC<{ isActive: boolean }> = ({ isActive }) => {
  const { postMessage, useMessage } = useVSCodeApi();

  const [input, setInput] = useState("");
  const [targetFolder, setTargetFolder] = useState("");
  const [contextFiles, setContextFiles] = useState<CtxFile[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [applied, setApplied] = useState<Record<string, boolean>>({});

  useMessage(useCallback((msg) => {
    const { type, payload } = msg;
    if (type === "code.result") {
      const res = payload as CodeResult;
      setTurns((ts) => {
        const copy = [...ts];
        for (let i = copy.length - 1; i >= 0; i--) {
          if (copy[i].status === "loading") { copy[i] = { ...copy[i], status: "done", result: res }; break; }
        }
        return copy;
      });
    } else if (type === "code.error") {
      const m = (payload as { message?: string })?.message ?? String(payload);
      setTurns((ts) => {
        const copy = [...ts];
        for (let i = copy.length - 1; i >= 0; i--) {
          if (copy[i].status === "loading") { copy[i] = { ...copy[i], status: "error", error: m }; break; }
        }
        return copy;
      });
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
    setTurns((ts) => [...ts, { id, prompt: text, status: "loading" }]);
    postMessage("code.generate", { instruction: text, targetFolder, contextFiles });
    setInput("");
  }, [input, targetFolder, contextFiles, postMessage]);

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

  if (!isActive) { return null; }

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

  return (
    <div style={{ borderTop: "1px solid var(--vscode-panel-border, #333)", margin: "16px 0 0", paddingTop: 14 }}>
      <div style={label}>코드 작성 및 수정</div>

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

          {turn.status === "loading" && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--vscode-descriptionForeground, #888)", fontSize: 11, padding: "2px 0 6px" }}>
              <div style={{ width: 11, height: 11, border: "2px solid #3f3f3f", borderTopColor: "var(--vscode-progressBar-background, #3794ff)", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} />
              <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
              생성 중…
            </div>
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
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { send(); } }}
        placeholder={turns.length ? "이어서 수정 요청 (예: 버튼 색을 파랑으로)" : "만들거나 고칠 내용을 입력 (예: 할 일 목록 앱)"}
        style={{ width: "100%", boxSizing: "border-box", minHeight: 48, background: "var(--vscode-input-background, #252526)", color: "var(--vscode-input-foreground, #ccc)", border: "1px solid var(--vscode-input-border, #3f3f3f)", borderRadius: 4, padding: "7px 9px", fontSize: 12, fontFamily: "var(--vscode-font-family, sans-serif)", resize: "vertical", outline: "none", lineHeight: 1.5 }}
      />
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 8 }}>
        <button onClick={send} disabled={!input.trim()} style={{ ...primaryBtn, opacity: input.trim() ? 1 : 0.5, cursor: input.trim() ? "pointer" : "not-allowed" }}>
          보내기
        </button>
        <span style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #777)" }}>Ctrl+Enter</span>
      </div>
    </div>
  );
};

export default CodeAgent;
