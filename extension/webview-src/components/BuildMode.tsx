/**
 * ReCoder — BuildMode component
 * Error analysis, patch proposals, diff preview, and approval UI.
 */

import React, { useState, useCallback, useRef } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";
import ApprovalModal, { RiskLevel } from "./ApprovalModal";
import CodeAgent from "./CodeAgent";

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

// ── Step Progress Bar ────────────────────────────────────────────────────────

type BuildStep = 0 | 1 | 2 | 3;

const STEPS = ["에러 수집", "코드 패치", "Dockerfile", "배포"];

const StepBar: React.FC<{ current: BuildStep }> = ({ current }) => {
  const green = "#22c55e";
  const blue = "#3b82f6";
  const gray = "#3f3f3f";

  return (
    <div style={{ padding: "10px 4px 14px", position: "relative" }}>
      {/* Connector line */}
      <div style={{
        position: "absolute",
        top: 20,
        left: "12.5%",
        right: "12.5%",
        height: 2,
        background: gray,
        zIndex: 0,
      }} />
      {/* Filled portion */}
      <div style={{
        position: "absolute",
        top: 20,
        left: "12.5%",
        width: `${(current / 3) * 75}%`,
        height: 2,
        background: blue,
        zIndex: 1,
        transition: "width 0.3s ease",
      }} />
      {/* Step circles + labels */}
      <div style={{ display: "flex", justifyContent: "space-between", position: "relative", zIndex: 2 }}>
        {STEPS.map((label, i) => {
          const done = i < current;
          const active = i === current;
          return (
            <div key={label} style={{ display: "flex", flexDirection: "column", alignItems: "center", flex: 1 }}>
              <div style={{
                width: 18,
                height: 18,
                borderRadius: "50%",
                border: `2px solid ${done ? green : active ? blue : gray}`,
                background: done ? green : active ? "rgba(59,130,246,0.2)" : "var(--vscode-sideBar-background, #1e1e1e)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 9,
                color: done ? "#fff" : active ? blue : "#555",
                fontWeight: 700,
                transition: "all 0.2s",
              }}>
                {done ? "✓" : i + 1}
              </div>
              <span style={{
                fontSize: 9,
                marginTop: 4,
                color: done ? green : active ? "#ccc" : "#555",
                fontWeight: active ? 600 : 400,
                textAlign: "center",
                whiteSpace: "nowrap",
              }}>
                {label}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
};

// ── BuildMode ───────────────────────────────────────────────────────────────

interface BuildModeProps {
  isActive: boolean;
  /** 큰 ReCoder 작업 화면에서는 오른쪽 고정 대화 패널을 사용한다. */
  showCodeAgent?: boolean;
}

export const BuildMode: React.FC<BuildModeProps> = ({ isActive, showCodeAgent = true }) => {
  const { postMessage, useMessage } = useVSCodeApi();

  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [proposal, setProposal] = useState<PatchProposal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showApproval, setShowApproval] = useState(false);
  const [approvalResult, setApprovalResult] = useState<"approved" | "rejected" | null>(null);


  // Derive current step
  const currentStep: BuildStep = proposal
    ? approvalResult === "approved"
      ? 3
      : 1
    : 0;

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

  // 자동 감지가 기본. 직접 누르면 현재 터미널 출력+선택을 모아 분석(명령과 동일 경로).
  const handleAnalyzeNow = useCallback(() => {
    setIsAnalyzing(true);
    setProposal(null);
    setError(null);
    setApprovalResult(null);
    postMessage("build.analyzeActive");
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
    low: "#22c55e",
    medium: "#f59e0b",
    high: "#ef4444",
    critical: "#7f1d1d",
  };

  return (
    <div style={{ fontFamily: "var(--vscode-font-family, sans-serif)", fontSize: 12, color: "var(--vscode-editor-foreground)" }}>

      {/* ── 에러 분석 ── */}
      <div style={{ fontSize: 11, fontWeight: 600, color: "var(--vscode-descriptionForeground, #888)", marginBottom: 6 }}>
        에러 분석
      </div>
      <div style={{ background: "var(--vscode-textCodeBlock-background, #1e1e1e)", border: "1px solid var(--vscode-panel-border, #333)", borderRadius: 6, padding: "9px 11px", fontSize: 11, color: "var(--vscode-descriptionForeground, #aaa)", lineHeight: 1.6 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4, color: "#22c55e", fontWeight: 600 }}>
          <span style={{ width: 7, height: 7, borderRadius: "50%", background: "#22c55e", display: "inline-block" }} />
          자동 감지 켜짐
        </div>
        코드 실행 중 에러가 나면 자동으로 분석합니다. 직접 분석하려면 터미널이나 에디터에서 에러를 선택하고{" "}
        <strong style={{ color: "var(--vscode-foreground, #ccc)" }}>우클릭 → ReCoder: Analyze Error</strong>.
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}>
        <button
          onClick={handleAnalyzeNow}
          disabled={isAnalyzing}
          style={{ background: "#2563eb", color: "#fff", border: "none", borderRadius: 5, padding: "6px 14px", fontSize: 12, fontWeight: 600, cursor: isAnalyzing ? "not-allowed" : "pointer", opacity: isAnalyzing ? 0.55 : 1 }}
        >
          지금 분석
        </button>
        <button
          onClick={() => postMessage("build.scan", {})}
          style={{ marginLeft: "auto", background: "#1c1c1c", color: "#ccc", border: "1px solid #3f3f3f", borderRadius: 5, padding: "6px 12px", fontSize: 12, fontWeight: 500, cursor: "pointer" }}
        >
          스캔
        </button>
      </div>

      {/* Loading */}
      {isAnalyzing && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 12, color: "var(--vscode-descriptionForeground, #888)" }}>
          <div style={{ width: 13, height: 13, border: "2px solid #3f3f3f", borderTopColor: "#3b82f6", borderRadius: "50%", animation: "spin 0.8s linear infinite", flexShrink: 0 }} />
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          <span>AI가 에러를 분석하는 중…</span>
        </div>
      )}

      {/* Error state */}
      {error && (
        <div style={{ marginTop: 10, background: "rgba(239,68,68,0.1)", border: "1px solid #ef4444", borderRadius: 5, padding: "8px 10px", color: "#ef4444" }}>
          {error}
        </div>
      )}

      {/* Approval result banner */}
      {approvalResult && (
        <div style={{
          marginTop: 10,
          background: approvalResult === "approved" ? "rgba(34,197,94,0.1)" : "rgba(239,68,68,0.1)",
          border: `1px solid ${approvalResult === "approved" ? "#22c55e" : "#ef4444"}`,
          borderRadius: 5,
          padding: "8px 10px",
          color: approvalResult === "approved" ? "#22c55e" : "#ef4444",
          fontWeight: 600,
        }}>
          {approvalResult === "approved" ? "✓ 패치가 성공적으로 적용되었습니다" : "✗ 패치가 거절되었습니다"}
        </div>
      )}

      {/* Proposal */}
      {proposal && !showApproval && (
        <>
          <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em", color: "#666", marginBottom: 6, marginTop: 14 }}>
            분석 결과
          </div>
          <div style={{
            background: "#252526",
            border: "1px solid #333",
            borderRadius: 5,
            padding: "8px 10px",
            marginBottom: 10,
          }}>
            <div style={{ marginBottom: 6, lineHeight: 1.5 }}>{proposal.summary}</div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{
                background: riskColors[proposal.risk_level],
                color: "#fff",
                borderRadius: 4,
                padding: "2px 7px",
                fontSize: 10,
                fontWeight: 700,
              }}>
                {proposal.risk_level.toUpperCase()}
              </span>
              <span style={{ fontSize: 10, color: "#666" }}>
                Level {proposal.approval_level} approval
              </span>
            </div>
            {proposal.risk_reasons.length > 0 && (
              <ul style={{ margin: "6px 0 0", paddingLeft: 16, fontSize: 11, color: "#888", lineHeight: 1.6 }}>
                {proposal.risk_reasons.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            )}
          </div>

          <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em", color: "#666", marginBottom: 6 }}>
            Diff 미리보기 ({proposal.patches.length}개 파일)
          </div>
          {proposal.patches.map((patch, i) => (
            <DiffPreview key={i} patch={patch} />
          ))}

          {proposal.test_command && (
            <>
              <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em", color: "#666", marginBottom: 6, marginTop: 10 }}>
                테스트 명령어
              </div>
              <div style={{
                background: "#1e1e1e",
                border: "1px solid #333",
                borderRadius: 4,
                padding: "6px 8px",
                fontFamily: "monospace",
                fontSize: 11,
                marginBottom: 10,
              }}>
                {proposal.test_command}
              </div>
            </>
          )}

          {!approvalResult && (
            <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
              <button
                style={{ background: "#2a2a2a", color: "#ccc", border: "1px solid #3f3f3f", borderRadius: 5, padding: "6px 12px", fontSize: 12, cursor: "pointer" }}
                onClick={handleReject}
              >
                거절
              </button>
              <button
                style={{ background: "#2563eb", color: "#fff", border: "none", borderRadius: 5, padding: "6px 14px", fontSize: 12, fontWeight: 600, cursor: "pointer" }}
                onClick={() => {
                  if (proposal.approval_level <= 1) {
                    handleApprove();
                  } else {
                    setShowApproval(true);
                  }
                }}
              >
                {proposal.approval_level <= 1 ? "패치 적용" : `승인 (L${proposal.approval_level})`}
              </button>
            </div>
          )}
        </>
      )}

      {/* Approval Modal */}
      {showApproval && proposal && (
        <div style={{ marginTop: 10 }}>
          <ApprovalModal
            level={proposal.approval_level}
            title={`${proposal.patches.length}개 패치 적용`}
            summary={proposal.summary}
            riskLevel={proposal.risk_level}
            riskReasons={proposal.risk_reasons}
            commandPreview={proposal.test_command}
            onApprove={handleApprove}
            onReject={handleReject}
          />
        </div>
      )}

      {showCodeAgent && <CodeAgent isActive={isActive} />}


    </div>
  );
};

export default BuildMode;
