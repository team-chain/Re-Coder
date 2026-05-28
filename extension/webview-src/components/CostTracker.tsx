/**
 * ReCoder — CostTracker (minimal)
 *
 * 한 줄 표시: "LLM $0.00 / $3 (0%)" + 클릭 시 details 펼침.
 * 이전엔 5개 row + budget bar + breakdown 등 정보 과잉.
 */

import React, { useState } from "react";

export interface ModelUsage {
  model: string;
  provider: string;
  call_count: number;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: number;
}

export interface CostSummaryData {
  daily_usd: number;
  monthly_usd: number;
  call_count: number;
  last_updated?: string;
}

export interface CostTrackerProps {
  costSummary?: CostSummaryData | null;
  dailyUsd?: number;
  monthlyUsd?: number;
  callCount?: number;
  lastUpdated?: string;
  modelBreakdown?: ModelUsage[];
  monthlyBudgetUsd?: number;
}

function formatUsd(amount: number): string {
  if (amount < 0.01 && amount > 0) return "<$0.01";
  return `$${amount.toFixed(2)}`;
}

export const CostTracker: React.FC<CostTrackerProps> = ({
  costSummary,
  dailyUsd: dailyUsdProp,
  monthlyUsd: monthlyUsdProp,
  callCount: callCountProp,
  monthlyBudgetUsd = 3,
}) => {
  const dailyUsd = costSummary?.daily_usd ?? dailyUsdProp ?? 0;
  const monthlyUsd = costSummary?.monthly_usd ?? monthlyUsdProp ?? 0;
  const callCount = costSummary?.call_count ?? callCountProp ?? 0;
  const [open, setOpen] = useState(false);

  const budgetPct = monthlyBudgetUsd
    ? Math.min((monthlyUsd / monthlyBudgetUsd) * 100, 100)
    : 0;

  const barColor =
    budgetPct > 90
      ? "var(--vscode-editorError-foreground, #f44)"
      : budgetPct > 70
      ? "var(--vscode-editorWarning-foreground, #fa0)"
      : "var(--vscode-button-background, #0078d4)";

  return (
    <div
      style={{
        fontSize: 11.5,
        color: "var(--vscode-descriptionForeground, #888)",
        padding: "6px 10px",
        display: "flex",
        flexDirection: "column",
        gap: 6,
        cursor: "pointer",
        userSelect: "none",
      }}
      onClick={() => setOpen((v) => !v)}
      title={open ? "접기" : "자세히 보기"}
    >
      {/* 한 줄 요약 */}
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span>LLM</span>
        <span
          style={{
            fontFamily: "var(--vscode-editor-font-family, monospace)",
            color: "var(--vscode-editor-foreground)",
            fontWeight: 600,
          }}
        >
          {formatUsd(monthlyUsd)}
        </span>
        <span style={{ opacity: 0.6 }}>/ {formatUsd(monthlyBudgetUsd)}</span>
        {/* 인라인 미니 progress bar */}
        <div
          style={{
            flex: 1,
            height: 3,
            background: "var(--vscode-progressBar-background, #333)",
            borderRadius: 2,
            overflow: "hidden",
            minWidth: 40,
            maxWidth: 100,
            marginLeft: 4,
          }}
        >
          <div
            style={{
              width: `${budgetPct}%`,
              height: "100%",
              background: barColor,
              transition: "width 0.4s ease",
            }}
          />
        </div>
        <span style={{ fontSize: 10, opacity: 0.7, minWidth: 28, textAlign: "right" }}>
          {budgetPct.toFixed(0)}%
        </span>
      </div>

      {/* 펼친 details — 클릭 시에만 */}
      {open && (
        <div
          style={{
            display: "flex",
            gap: 14,
            fontSize: 10.5,
            opacity: 0.85,
            paddingTop: 4,
            borderTop: "1px solid var(--vscode-panel-border, #333)",
          }}
        >
          <span>오늘 {formatUsd(dailyUsd)}</span>
          <span>호출 {callCount.toLocaleString()}</span>
        </div>
      )}
    </div>
  );
};

export default CostTracker;
