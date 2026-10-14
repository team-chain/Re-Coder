/**
 * ReCoder — ApprovalModal component
 * Renders approval UI for Level 1–4, with escalating confirmation requirements.
 */

import React, { useState, useCallback } from "react";
import { issueKind, shortIssue } from "./issueText";
import { Checkbox } from "./Check";

/** 실제로 실행할 명령 한 단계(코어가 실행 경로와 같은 함수로 만든 것, 설정값은 *** 로 가림). */
export interface CommandStep { command: string; note?: string }

export type RiskLevel = "low" | "medium" | "high" | "critical";

export interface ApprovalModalProps {
  level: 1 | 2 | 3 | 4;
  title: string;
  summary: string;
  riskLevel: RiskLevel;
  riskReasons: string[];
  commandPreview?: string;     // Level 2+ (코어가 단계 목록을 주지 않을 때의 예전 한 줄)
  commandSteps?: CommandStep[]; // Level 2+
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
  low: "낮은 위험",
  medium: "중간 위험",
  high: "높은 위험",
  critical: "매우 높은 위험",
};

const LEVEL_LABELS: Record<number, string> = {
  1: "자동 승인",
  2: "확인 필요",
  3: "두 번 확인",
  4: "차단됨 — 직접 확인 후 진행",
};

const CONFIRM_KEYWORD = "CONFIRM";

export const ApprovalModal: React.FC<ApprovalModalProps> = ({
  level,
  title,
  summary,
  riskLevel,
  riskReasons,
  commandPreview,
  commandSteps,
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
    fontSize: 11,
    fontWeight: 700,
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
            승인 단계 {level} · {LEVEL_LABELS[level]}
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
        <div style={labelStyle}>요약</div>
        <div style={{ lineHeight: 1.5, color: "var(--vscode-editor-foreground)" }}>{summary}</div>
      </div>

      {/* Risk Reasons */}
      {riskReasons.length > 0 && (
        <div style={sectionStyle}>
          <div style={labelStyle}>위험 사유</div>
          {/* 사유마다 첫 문장만 한 줄로 — 같은 사유가 앞 화면에도 나온다. 원문은 접어 둔다. */}
          <ul style={{ margin: 0, paddingLeft: 16, lineHeight: 1.6 }}>
            {riskReasons.filter((r) => issueKind(r) !== "autofix").map((r, i) => (
              <li key={i} style={{ color: riskColor }} title={r}>{shortIssue(r)}</li>
            ))}
            {riskReasons.some((r) => issueKind(r) === "autofix") && (
              <li style={{ color: riskColor }}>배포 전 자동 수정 {riskReasons.filter((r) => issueKind(r) === "autofix").length}건 — 배포할 때 고치고 원본은 .recoder/backups 에 남깁니다.</li>
            )}
          </ul>
          {riskReasons.some((r) => shortIssue(r) !== r.trim()) && (
            <details style={{ marginTop: 4, color: "var(--vscode-descriptionForeground, #999)" }}>
              <summary style={{ cursor: "pointer" }}>원문 보기</summary>
              <ul style={{ margin: "4px 0 0", paddingLeft: 16, lineHeight: 1.5 }}>{riskReasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
            </details>
          )}
        </div>
      )}

      {/* 실행할 명령 (Level 2+) — 코어가 실제 실행 순서대로 준 목록. 없으면 예전 한 줄. */}
      {level >= 2 && commandSteps && commandSteps.length > 0 && (
        <div style={sectionStyle} data-testid="command-steps">
          <div style={labelStyle}>실행할 명령 · 순서대로 (설정값은 가림)</div>
          <ol style={{ margin: 0, padding: 0, listStyle: "none", border: "1px solid var(--vscode-panel-border, #333)", borderRadius: 5, background: "var(--vscode-textCodeBlock-background, #1e1e1e)", maxHeight: 260, overflowY: "auto" }}>
            {commandSteps.map((step, i) => (
              <li key={i} style={{ display: "grid", gridTemplateColumns: "18px minmax(0,1fr)", columnGap: 6, padding: "5px 8px", borderTop: i ? "1px solid var(--vscode-panel-border, #2a2a2a)" : undefined }}>
                <span style={{ color: "var(--vscode-descriptionForeground, #888)", textAlign: "right", fontFamily: "var(--vscode-editor-font-family, monospace)", fontSize: 11 }}>{i + 1}</span>
                <code style={{ fontFamily: "var(--vscode-editor-font-family, monospace)", fontSize: 11, whiteSpace: "pre-wrap", wordBreak: "break-all", background: "transparent", padding: 0, color: "var(--vscode-editor-foreground, #d4d4d4)" }}>{step.command}</code>
                {step.note && <span style={{ gridColumn: 2, fontSize: 10.5, color: "var(--vscode-descriptionForeground, #999)", marginTop: 1 }}>{step.note}</span>}
              </li>
            ))}
          </ol>
        </div>
      )}
      {level >= 2 && !(commandSteps && commandSteps.length) && commandPreview && (
        <div style={sectionStyle}>
          <div style={labelStyle}>실행할 명령</div>
          <div style={codeBlockStyle}>{commandPreview}</div>
        </div>
      )}

      {/* Affected Targets (Level 3+) */}
      {level >= 3 && affectedTargets && affectedTargets.length > 0 && (
        <div style={sectionStyle}>
          <div style={labelStyle}>영향받는 대상</div>
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
          <div style={labelStyle}>되돌리는 방법</div>
          <div style={{ ...codeBlockStyle, maxHeight: 40 }}>{rollbackPath}</div>
        </div>
      )}

      {/* Diff Preview (Level 4) */}
      {level >= 4 && (diffBefore || diffAfter) && (
        <div style={sectionStyle}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
            <div style={labelStyle}>바뀌는 내용</div>
            <button
              style={{ ...buttonBase, background: "transparent", border: "1px solid var(--vscode-panel-border, #444)", color: "var(--vscode-editor-foreground)", padding: "2px 8px", fontSize: 10 }}
              onClick={() => setShowDiff((v) => !v)}
            >
              {showDiff ? "변경 숨기기" : "변경 보기"}
            </button>
          </div>
          {showDiff && (
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
              <div>
                <div style={{ ...labelStyle, color: "var(--vscode-editorError-foreground, #f44)" }}>바꾸기 전</div>
                <div style={{ ...codeBlockStyle, borderColor: "var(--vscode-editorError-foreground, #f44)" }}>
                  {diffBefore ?? "(비어 있음)"}
                </div>
              </div>
              <div>
                <div style={{ ...labelStyle, color: "var(--vscode-testing-iconPassed, #4af)" }}>바꾼 뒤</div>
                <div style={{ ...codeBlockStyle, borderColor: "var(--vscode-testing-iconPassed, #4af)" }}>
                  {diffAfter ?? "(비어 있음)"}
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Level 3: 두 번째 확인 — 사유를 읽었다는 체크 */}
      {level === 3 && (
        <div style={{ ...sectionStyle, padding: "8px 10px", borderRadius: 4, border: `1px solid ${riskColor}`, background: "rgba(245,158,11,0.06)" }}>
          <Checkbox
            checked={acknowledged}
            onChange={setAcknowledged}
            ariaLabel="위험 사유 확인"
            tone="warn"
            label={`위 위험 사유${riskReasons.length > 0 ? ` ${riskReasons.length}건` : ""}을 읽었고, 이 상태로 실행하는 데 동의합니다.`}
            description={riskReasons.some((r) => /미검증|unverified|검사 실패|not.?run/i.test(r))
              ? <span style={{ color: riskColor }}>검사가 통과된 것이 아니라 확인하지 못한 상태입니다.</span>
              : "확인해야 [승인] 이 눌립니다."}
          />
        </div>
      )}

      {/* Level 4: Confirmation typing */}
      {level >= 4 && (
        <div style={sectionStyle}>
          <div style={labelStyle}>
            승인하려면 <strong style={{ color: riskColor }}>{CONFIRM_KEYWORD}</strong> 를 입력하세요
          </div>
          <input
            type="text"
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            placeholder={`${CONFIRM_KEYWORD} 입력`}
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
          거절
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
          {level >= 4 ? "차단을 넘기고 승인" : "승인"}
        </button>
      </div>
    </div>
  );
};

export default ApprovalModal;
