/**
 * ReCoder — BuildMode component
 * Error analysis, patch proposals, diff preview, and approval UI.
 */

import React, { useState, useCallback, useRef } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import ApprovalModal, { RiskLevel } from "./ApprovalModal";

// ── Types ──────────────────────────────────────────────────────────────────

interface FilePatch {
  file: string;
  base_sha256?: string;
  unified_diff: string;
  reason: string;
}

interface PatchProposal {
  proposal_id: string;
  summary: string;
  risk_level: RiskLevel;
  risk_reasons: string[];
  approval_level: 1 | 2 | 3 | 4;
  patches: FilePatch[];
  test_command?: string;
}

// ── Diff Preview ────────────────────────────────────────────────────────────

interface DiffPreviewProps {
  patch: FilePatch;
}

const DiffPreview: React.FC<DiffPreviewProps> = ({ patch }) => {
  const lines = patch.unified_diff.split("\n");

  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{
        background: "var(--vscode-editorGroupHeader-tabsBackground, #2d2d2d)",
        padding: "3px 8px",
        fontSize: 11,
        fontFamily: "var(--vscode-editor-font-family, monospace)",
        color: "var(--vscode-tab-activeForeground, #ccc)",
        borderRadius: "3px 3px 0 0",
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
      }}>
        <span>─── {patch.file} ───</span>
        {patch.base_sha256 && (
          <span style={{ fontSize: 9, color: "var(--vscode-descriptionForeground, #666)", fontFamily: "monospace" }}>
            sha: {patch.base_sha256.substring(0, 8)}
          </span>
        )}
      </div>
      <div style={{
        background: "var(--vscode-textCodeBlock-background, #1e1e1e)",
        border: "1px solid var(--vscode-panel-border, #333)",
        borderTop: "none",
        borderRadius: "0 0 3px 3px",
        fontFamily: "var(--vscode-editor-font-family, monospace)",
        fontSize: 11,
        overflowX: "auto",
        maxHeight: 200,
        overflowY: "auto",
      }}>
        {lines.map((line, i) => {
          let bg = "transparent";
          let color = "var(--vscode-editor-foreground)";

          if (line.startsWith("+") && !line.startsWith("+++")) {
            bg = "rgba(76, 175, 80, 0.15)";
            color = "var(--vscode-testing-iconPassed, #81c784)";
          } else if (line.startsWith("-") && !line.startsWith("---")) {
            bg = "rgba(244, 67, 54, 0.12)";
            color = "var(--vscode-editorError-foreground, #e57373)";
          } else if (line.startsWith("@@")) {
            bg = "rgba(0, 120, 212, 0.1)";
            color = "var(--vscode-textLink-foreground, #79b8ff)";
          } else if (line.startsWith("---") || line.startsWith("+++")) {
            color = "var(--vscode-descriptionForeground, #888)";
          }

          return (
            <div key={i} style={{ background: bg, color, padding: "0 8px", whiteSpace: "pre", lineHeight: 1.7 }}>
              {line || " "}
            </div>
          );
        })}
      </div>
      {patch.reason && (
        <div style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #888)", padding: "3px 4px", fontStyle: "italic" }}>
          Reason: {patch.reason}
        </div>
      )}
    </div>
  );
};

// ── BuildMode ───────────────────────────────────────────────────────────────

interface BuildModeProps {
  isActive: boolean;
}

export const BuildMode: React.FC<BuildModeProps> = ({ isActive }) => {
  const { postMessage, useMessage } = useVSCodeApi();

  const [errorLog, setErrorLog] = useState("");
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [proposal, setProposal] = useState<PatchProposal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showApproval, setShowApproval] = useState(false);
  const [approvalResult, setApprovalResult] = useState<"approved" | "rejected" | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Listen for messages from extension host
  useMessage(useCallback((msg) => {
    const { type, payload } = msg;
    if (type === "build.analysis.result") {
      setProposal(payload as PatchProposal);
      setIsAnalyzing(false);
      setError(null);
    }
    if (type === "build.analysis.error") {
      setError(String(payload));
      setIsAnalyzing(false);
    }
    if (type === "build.patch.applied") {
      setApprovalResult("approved");
      setShowApproval(false);
    }
    if (type === "build.patch.rejected") {
      setApprovalResult("rejected");
      setShowApproval(false);
    }
  }, []));

  const handleAnalyze = useCallback(() => {
    if (!errorLog.trim()) return;
    setIsAnalyzing(true);
    setProposal(null);
    setError(null);
    setApprovalResult(null);
    postMessage("build.analyze", { error_log: errorLog });
  }, [errorLog, postMessage]);

  const handlePaste = useCallback(async () => {
    try {
      const text = await navigator.clipboard.readText();
      setErrorLog(text);
      textareaRef.current?.focus();
    } catch {
      textareaRef.current?.focus();
      postMessage("webview.paste.request");
    }
  }, [postMessage]);

  const handleApprove = useCallback(() => {
    if (!proposal) return;
    postMessage("build.patch.approve", { proposal_id: proposal.proposal_id });
  }, [proposal, postMessage]);

  const handleReject = useCallback(() => {
    if (!proposal) return;
    postMessage("build.patch.reject", { proposal_id: proposal.proposal_id });
    setShowApproval(false);
  }, [proposal, postMessage]);

  // ── Styles ────────────────────────────────────────────────────────────────

  const sectionHeader: React.CSSProperties = {
    fontSize: 10,
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    color: "var(--vscode-descriptionForeground, #888)",
    marginBottom: 6,
    marginTop: 12,
  };

  const btnPrimary: React.CSSProperties = {
    background: "var(--vscode-button-background, #0078d4)",
    color: "var(--vscode-button-foreground, #fff)",
    border: "none",
    borderRadius: 4,
    padding: "6px 12px",
    fontSize: 12,
    cursor: "pointer",
    fontWeight: 600,
  };

  const btnSecondary: React.CSSProperties = {
    background: "var(--vscode-button-secondaryBackground, #3a3a3a)",
    color: "var(--vscode-button-secondaryForeground, #ccc)",
    border: "none",
    borderRadius: 4,
    padding: "6px 12px",
    fontSize: 12,
    cursor: "pointer",
  };

  if (!isActive) {
    return (
      <div style={{ padding: 16, color: "var(--vscode-descriptionForeground, #888)", textAlign: "center", fontSize: 12 }}>
        Build Mode requires Core Ready + AI Ready.{" "}
        <a href="#" style={{ color: "var(--vscode-textLink-foreground, #4af)" }}>See diagnostics</a>
      </div>
    );
  }

  const riskColors: Record<RiskLevel, string> = {
    low: "var(--vscode-testing-iconPassed, #4caf50)",
    medium: "var(--vscode-editorWarning-foreground, #ff9800)",
    high: "var(--vscode-editorError-foreground, #f44336)",
    critical: "#b71c1c",
  };

  return (
    <div style={{ padding: "0 2px", fontFamily: "var(--vscode-font-family, sans-serif)", fontSize: 12, color: "var(--vscode-editor-foreground)" }}>

      {/* Error Log Input */}
      <div style={sectionHeader}>Error Log</div>
      <textarea
        ref={textareaRef}
        value={errorLog}
        onChange={(e) => setErrorLog(e.target.value)}
        placeholder="Paste error log here, or use the button below…"
        style={{
          width: "100%",
          boxSizing: "border-box",
          minHeight: 90,
          background: "var(--vscode-input-background, #1e1e1e)",
          color: "var(--vscode-input-foreground, #ccc)",
          border: "1px solid var(--vscode-input-border, #555)",
          borderRadius: 3,
          padding: "6px 8px",
          fontSize: 11,
          fontFamily: "var(--vscode-editor-font-family, monospace)",
          resize: "vertical",
          outline: "none",
        }}
      />

      <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
        <button style={btnSecondary} onClick={handlePaste}>
          Paste Error Log
        </button>
        <button
          style={{ ...btnPrimary, opacity: (!errorLog.trim() || isAnalyzing) ? 0.6 : 1, cursor: (!errorLog.trim() || isAnalyzing) ? "not-allowed" : "pointer" }}
          onClick={handleAnalyze}
          disabled={!errorLog.trim() || isAnalyzing}
        >
          {isAnalyzing ? "Analyzing…" : "Analyze Error"}
        </button>
      </div>

      {/* Loading */}
      {isAnalyzing && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 12, color: "var(--vscode-descriptionForeground, #888)" }}>
          <div style={{ width: 14, height: 14, border: "2px solid var(--vscode-panel-border, #444)", borderTopColor: "var(--vscode-button-background, #0078d4)", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} />
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          <span>AI is analyzing the error…</span>
        </div>
      )}

      {/* Error state */}
      {error && (
        <div style={{ marginTop: 10, background: "rgba(244, 67, 54, 0.1)", border: "1px solid var(--vscode-editorError-foreground, #f44)", borderRadius: 4, padding: "8px 10px", color: "var(--vscode-editorError-foreground, #f44)" }}>
          {error}
        </div>
      )}

      {/* Approval result banner */}
      {approvalResult && (
        <div style={{
          marginTop: 10,
          background: approvalResult === "approved" ? "rgba(76, 175, 80, 0.12)" : "rgba(244, 67, 54, 0.1)",
          border: `1px solid ${approvalResult === "approved" ? "var(--vscode-testing-iconPassed, #4caf50)" : "var(--vscode-editorError-foreground, #f44)"}`,
          borderRadius: 4,
          padding: "8px 10px",
          color: approvalResult === "approved" ? "var(--vscode-testing-iconPassed, #4caf50)" : "var(--vscode-editorError-foreground, #f44)",
          fontWeight: 600,
        }}>
          {approvalResult === "approved" ? "✓ Patch applied successfully" : "✗ Patch rejected"}
        </div>
      )}

      {/* Proposal */}
      {proposal && !showApproval && (
        <>
          {/* Summary & Risk */}
          <div style={sectionHeader}>Analysis Result</div>
          <div style={{
            background: "var(--vscode-sideBar-background, #252526)",
            border: "1px solid var(--vscode-panel-border, #333)",
            borderRadius: 4,
            padding: "8px 10px",
            marginBottom: 10,
          }}>
            <div style={{ marginBottom: 6, lineHeight: 1.5 }}>{proposal.summary}</div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{
                background: riskColors[proposal.risk_level],
                color: "#fff",
                borderRadius: 3,
                padding: "2px 7px",
                fontSize: 10,
                fontWeight: 700,
              }}>
                {proposal.risk_level.toUpperCase()}
              </span>
              <span style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #888)" }}>
                Level {proposal.approval_level} approval
              </span>
            </div>
            {proposal.risk_reasons.length > 0 && (
              <ul style={{ margin: "6px 0 0", paddingLeft: 16, fontSize: 11, color: "var(--vscode-descriptionForeground, #888)", lineHeight: 1.6 }}>
                {proposal.risk_reasons.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            )}
          </div>

          {/* Diff Preview */}
          <div style={sectionHeader}>Diff Preview ({proposal.patches.length} file{proposal.patches.length !== 1 ? "s" : ""})</div>
          {proposal.patches.map((patch, i) => (
            <DiffPreview key={i} patch={patch} />
          ))}

          {/* Test Command */}
          {proposal.test_command && (
            <>
              <div style={sectionHeader}>Test Command</div>
              <div style={{
                background: "var(--vscode-textCodeBlock-background, #1e1e1e)",
                border: "1px solid var(--vscode-panel-border, #333)",
                borderRadius: 3,
                padding: "6px 8px",
                fontFamily: "var(--vscode-editor-font-family, monospace)",
                fontSize: 11,
                marginBottom: 10,
              }}>
                {proposal.test_command}
              </div>
            </>
          )}

          {/* Approval buttons */}
          {!approvalResult && (
            <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
              <button style={btnSecondary} onClick={handleReject}>
                Reject
              </button>
              <button style={btnPrimary} onClick={() => {
                if (proposal.approval_level <= 1) {
                  handleApprove();
                } else {
                  setShowApproval(true);
                }
              }}>
                {proposal.approval_level <= 1 ? "Apply Patch" : `Approve (L${proposal.approval_level})`}
              </button>
            </div>
          )}
        </>
      )}

      {/* Approval Modal */}
      {showApproval && proposal && (
        <div style={{ marginTop: 10 }}>
          <div style={sectionHeader}>Approval Required</div>
          <ApprovalModal
            level={proposal.approval_level}
            title={`Apply ${proposal.patches.length} patch${proposal.patches.length !== 1 ? "es" : ""}`}
            summary={proposal.summary}
            riskLevel={proposal.risk_level}
            riskReasons={proposal.risk_reasons}
            commandPreview={proposal.test_command}
            onApprove={handleApprove}
            onReject={handleReject}
          />
        </div>
      )}

      {/* Empty state */}
      {!proposal && !isAnalyzing && !error && (
        <div style={{ marginTop: 16, textAlign: "center", color: "var(--vscode-descriptionForeground, #888)", fontSize: 11, lineHeight: 1.6 }}>
          Paste an error log and click Analyze to generate a patch proposal.
        </div>
      )}
    </div>
  );
};

export default BuildMode;
