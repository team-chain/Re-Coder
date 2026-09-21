/**
 * ReCoder — ApprovalModal component
 * Renders approval UI for Level 1–4, with escalating confirmation requirements.
 */

import React, { useState, useCallback } from "react";

export type RiskLevel = "low" | "medium" | "high" | "critical";

export interface ApprovalModalProps {
  level: 1 | 2 | 3 | 4;
  title: string;
  summary: string;
  riskLevel: RiskLevel;
  riskReasons: string[];
  commandPreview?: string;     // Level 2+
  affectedTargets?: string[];  // Level 3+
  rollbackPath?: string;       // Level 3+
  diffBefore?: string;         // Level 4
  diffAfter?: string;          // Level 4
  onApprove: () => void;
  onReject: () => void;
}

const RISK_COLORS: Record<RiskLevel, string> = {
  low: "var(--vscode-testing-iconPassed, #4caf50)",
  medium: "var(--vscode-editorWarning-foreground, #ff9800)",
  high: "var(--vscode-editorError-foreground, #f44336)",
  critical: "#b71c1c",
};

const RISK_LABELS: Record<RiskLevel, string> = {
  low: "LOW",
  medium: "MEDIUM",
  high: "HIGH",
  critical: "CRITICAL",
};

const LEVEL_LABELS: Record<number, string> = {
  1: "Auto Approval",
  2: "Confirm Required",
  3: "Double Confirm",
  4: "Blocked — Manual Override",
};

const CONFIRM_KEYWORD = "CONFIRM";

export const ApprovalModal: React.FC<ApprovalModalProps> = ({
  level,
  title,
  summary,
  riskLevel,
  riskReasons,
  commandPreview,
  affectedTargets,
  rollbackPath,
  diffBefore,
  diffAfter,
  onApprove,
  onReject,
}) => {
  const [confirmText, setConfirmText] = useState("");
  const [showDiff, setShowDiff] = useState(false);
  //: Level 3 "Double Confirm" 의 두 번째 단계. 예전엔 라벨만 "Double Confirm" 이고
  //: 버튼은 한 번에 눌렸다 — 코어가 미검증 배포를 이중 확인으로 올려도 화면에서는
  //: 아무 차이가 없었다(실기기 검증 B3). 사유를 읽었다는 체크가 있어야 승인이 열린다.
  const [acknowledged, setAcknowledged] = useState(false);

  const riskColor = RISK_COLORS[riskLevel];
  const isLevel4Confirmed =
    level < 4 || confirmText.trim().toUpperCase() === CONFIRM_KEYWORD;
  const isLevel3Confirmed = level < 3 || level >= 4 || acknowledged;
  const canApprove = isLevel4Confirmed && isLevel3Confirmed;

  const handleApprove = useCallback(() => {
    if (canApprove) {
      onApprove();
    }
  }, [canApprove, onApprove]);

  const containerStyle: React.CSSProperties = {
    background: "var(--vscode-editor-background)",
    border: "1px solid var(--vscode-panel-border, #444)",
    borderRadius: 6,
    padding: "12px 14px",
    fontSize: 12,
    color: "var(--vscode-editor-foreground)",
    fontFamily: "var(--vscode-font-family, sans-serif)",
  };

  const sectionStyle: React.CSSProperties = {
    marginBottom: 10,
  };

  const labelStyle: React.CSSProperties = {
    fontSize: 10,
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    color: "var(--vscode-descriptionForeground, #888)",
    marginBottom: 3,
  };

  const codeBlockStyle: React.CSSProperties = {
    background: "var(--vscode-textCodeBlock-background, #1e1e1e)",
    border: "1px solid var(--vscode-panel-border, #333)",
    borderRadius: 3,
    padding: "6px 8px",
    fontFamily: "var(--vscode-editor-font-family, monospace)",
    fontSize: 11,
    whiteSpace: "pre-wrap",
    wordBreak: "break-all",
    maxHeight: 120,
    overflowY: "auto",
  };

  const buttonBase: React.CSSProperties = {
    border: "none",
    borderRadius: 4,
    padding: "6px 14px",
    fontSize: 12,
    cursor: "pointer",
    fontWeight: 600,
  };

  return (
    <div style={containerStyle}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 10 }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 2 }}>{title}</div>
          <div style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #888)" }}>
            Approval Level {level} — {LEVEL_LABELS[level]}
          </div>
        </div>
        <span
          style={{
            background: riskColor,
            color: "#fff",
            borderRadius: 3,
            padding: "2px 7px",
            fontSize: 10,
            fontWeight: 700,
            flexShrink: 0,
          }}
        >
          {RISK_LABELS[riskLevel]}
        </span>
      </div>

      {/* Summary */}
      <div style={sectionStyle}>
        <div style={labelStyle}>Summary</div>
        <div style={{ lineHeight: 1.5, color: "var(--vscode-editor-foreground)" }}>{summary}</div>
      </div>

      {/* Risk Reasons */}
      {riskReasons.length > 0 && (
        <div style={sectionStyle}>
          <div style={labelStyle}>Risk Reasons</div>
          <ul style={{ margin: 0, paddingLeft: 16, lineHeight: 1.6 }}>
            {riskReasons.map((r, i) => (
              <li key={i} style={{ color: riskColor }}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Command Preview (Level 2+) */}
      {level >= 2 && commandPreview && (
        <div style={sectionStyle}>
          <div style={labelStyle}>Command Preview</div>
          <div style={codeBlockStyle}>{commandPreview}</div>
        </div>
      )}

      {/* Affected Targets (Level 3+) */}
      {level >= 3 && affectedTargets && affectedTargets.length > 0 && (
        <div style={sectionStyle}>
          <div style={labelStyle}>Affected Targets</div>
          {affectedTargets.map((t, i) => (
            <div
              key={i}
              style={{
                background: "var(--vscode-badge-background, #3a3a3a)",
                borderRadius: 3,
                padding: "2px 6px",
                display: "inline-block",
                marginRight: 4,
                marginBottom: 4,
                fontSize: 11,
              }}
            >
              {t}
            </div>
          ))}
        </div>
      )}

      {/* Rollback Path (Level 3+) */}
      {level >= 3 && rollbackPath && (
        <div style={sectionStyle}>
          <div style={labelStyle}>Rollback Path</div>
          <div style={{ ...codeBlockStyle, maxHeight: 40 }}>{rollbackPath}</div>
        </div>
      )}

      {/* Diff Preview (Level 4) */}
      {level >= 4 && (diffBefore || diffAfter) && (
        <div style={sectionStyle}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
            <div style={labelStyle}>Change Diff</div>
            <button
              style={{ ...buttonBase, background: "transparent", border: "1px solid var(--vscode-panel-border, #444)", color: "var(--vscode-editor-foreground)", padding: "2px 8px", fontSize: 10 }}
              onClick={() => setShowDiff((v) => !v)}
            >
              {showDiff ? "Hide" : "Show"} Diff
            </button>
          </div>
          {showDiff && (
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
              <div>
                <div style={{ ...labelStyle, color: "var(--vscode-editorError-foreground, #f44)" }}>Before</div>
                <div style={{ ...codeBlockStyle, borderColor: "var(--vscode-editorError-foreground, #f44)" }}>
                  {diffBefore ?? "(empty)"}
                </div>
              </div>
              <div>
                <div style={{ ...labelStyle, color: "var(--vscode-testing-iconPassed, #4af)" }}>After</div>
                <div style={{ ...codeBlockStyle, borderColor: "var(--vscode-testing-iconPassed, #4af)" }}>
                  {diffAfter ?? "(empty)"}
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Level 3: 두 번째 확인 — 사유를 읽었다는 체크 */}
      {level === 3 && (
        <div style={{ ...sectionStyle, padding: "8px 10px", borderRadius: 4, border: `1px solid ${riskColor}`, background: "rgba(245,158,11,0.06)" }}>
          <label style={{ display: "flex", alignItems: "flex-start", gap: 8, cursor: "pointer", lineHeight: 1.5 }}>
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(e) => setAcknowledged(e.target.checked)}
              style={{ marginTop: 2 }}
              aria-label="위험 사유 확인"
            />
            <span>
              위 위험 사유{riskReasons.length > 0 ? ` ${riskReasons.length}건` : ""}을 읽었고, 이 상태로 실행하는 데 동의합니다.
              {riskReasons.some((r) => /미검증|unverified|검사 실패|not.?run/i.test(r)) && (
                <span style={{ display: "block", color: riskColor, marginTop: 2 }}>
                  검사가 통과된 것이 아니라 확인하지 못한 상태입니다.
                </span>
              )}
            </span>
          </label>
        </div>
      )}

      {/* Level 4: Confirmation typing */}
      {level >= 4 && (
        <div style={sectionStyle}>
          <div style={labelStyle}>
            Type <strong style={{ color: riskColor }}>{CONFIRM_KEYWORD}</strong> to enable approval
          </div>
          <input
            type="text"
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            placeholder={`Type "${CONFIRM_KEYWORD}" to proceed`}
            style={{
              width: "100%",
              boxSizing: "border-box",
              background: "var(--vscode-input-background, #1e1e1e)",
              border: `1px solid ${confirmText.trim().toUpperCase() === CONFIRM_KEYWORD ? riskColor : "var(--vscode-input-border, #555)"}`,
              color: "var(--vscode-input-foreground, #ccc)",
              borderRadius: 3,
              padding: "5px 8px",
              fontSize: 12,
              fontFamily: "var(--vscode-editor-font-family, monospace)",
              outline: "none",
            }}
          />
        </div>
      )}

      {/* Action Buttons */}
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 12 }}>
        <button
          style={{
            ...buttonBase,
            background: "var(--vscode-button-secondaryBackground, #3a3a3a)",
            color: "var(--vscode-button-secondaryForeground, #ccc)",
          }}
          onClick={onReject}
        >
          Reject
        </button>
        <button
          style={{
            ...buttonBase,
            background: canApprove
              ? "var(--vscode-button-background, #0078d4)"
              : "var(--vscode-button-secondaryBackground, #3a3a3a)",
            color: canApprove
              ? "var(--vscode-button-foreground, #fff)"
              : "var(--vscode-disabledForeground, #666)",
            cursor: canApprove ? "pointer" : "not-allowed",
          }}
          onClick={handleApprove}
          disabled={!canApprove}
        >
          {level >= 4 ? "Override & Approve" : "Approve"}
        </button>
      </div>
    </div>
  );
};

export default ApprovalModal;
