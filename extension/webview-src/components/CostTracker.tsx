/**
 * ReCoder — CostTracker component
 * Displays daily / monthly LLM cost, call counts, and per-model breakdown.
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
  /** Pass either a CostSummary object from the Core API, or individual props */
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

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function estimateMonthly(daily: number): number {
  return daily * 30;
}

export const CostTracker: React.FC<CostTrackerProps> = ({
  costSummary,
  dailyUsd: dailyUsdProp,
  monthlyUsd: monthlyUsdProp,
  callCount: callCountProp,
  lastUpdated: lastUpdatedProp,
  modelBreakdown = [],
  monthlyBudgetUsd = 3, // 설계서 목표: 월 $1~3
}) => {
  // Resolve values from costSummary or individual props
  const dailyUsd = costSummary?.daily_usd ?? dailyUsdProp ?? 0;
  const monthlyUsd = costSummary?.monthly_usd ?? monthlyUsdProp ?? 0;
  const callCount = costSummary?.call_count ?? callCountProp ?? 0;
  const lastUpdated = costSummary?.last_updated ?? lastUpdatedProp;
  const [expanded, setExpanded] = useState(false);

  const projected = estimateMonthly(dailyUsd);
  const budgetPct = monthlyBudgetUsd ? Math.min((monthlyUsd / monthlyBudgetUsd) * 100, 100) : null;

  const rowStyle: React.CSSProperties = {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    padding: "4px 0",
    borderBottom: "1px solid var(--vscode-panel-border, #333)",
    fontSize: 12,
  };

  const labelStyle: React.CSSProperties = {
    color: "var(--vscode-descriptionForeground, #888)",
  };

  const valueStyle: React.CSSProperties = {
    fontFamily: "var(--vscode-editor-font-family, monospace)",
    fontWeight: 600,
    color: "var(--vscode-editor-foreground)",
  };

  const sectionHeader: React.CSSProperties = {
    fontSize: 10,
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    color: "var(--vscode-descriptionForeground, #888)",
    marginBottom: 6,
    marginTop: 10,
  };

  return (
    <div
      style={{
        background: "var(--vscode-editor-background)",
        border: "1px solid var(--vscode-panel-border, #444)",
        borderRadius: 6,
        padding: "10px 12px",
        fontSize: 12,
        color: "var(--vscode-editor-foreground)",
        fontFamily: "var(--vscode-font-family, sans-serif)",
      }}
    >
      {/* Title row */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <span style={{ fontWeight: 700, fontSize: 13 }}>LLM Cost Tracker</span>
        {lastUpdated && (
          <span style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #888)" }}>
            Updated {new Date(lastUpdated).toLocaleTimeString()}
          </span>
        )}
      </div>

      {/* Core stats */}
      <div style={rowStyle}>
        <span style={labelStyle}>Today</span>
        <span style={valueStyle}>{formatUsd(dailyUsd)}</span>
      </div>
      <div style={rowStyle}>
        <span style={labelStyle}>This month</span>
        <span style={valueStyle}>{formatUsd(monthlyUsd)}</span>
      </div>
      <div style={rowStyle}>
        <span style={labelStyle}>Projected (30d)</span>
        <span style={{ ...valueStyle, color: projected > (monthlyBudgetUsd ?? Infinity) ? "var(--vscode-editorError-foreground, #f44)" : "var(--vscode-editor-foreground)" }}>
          {formatUsd(projected)}
        </span>
      </div>
      <div style={{ ...rowStyle, borderBottom: "none" }}>
        <span style={labelStyle}>Total calls</span>
        <span style={valueStyle}>{callCount.toLocaleString()}</span>
      </div>

      {/* Budget progress bar */}
      {budgetPct !== null && (
        <div style={{ marginTop: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, marginBottom: 3 }}>
            <span style={labelStyle}>Budget usage</span>
            <span style={labelStyle}>{budgetPct.toFixed(1)}% of {formatUsd(monthlyBudgetUsd!)}</span>
          </div>
          <div style={{ background: "var(--vscode-progressBar-background, #333)", borderRadius: 3, height: 5, overflow: "hidden" }}>
            <div
              style={{
                width: `${budgetPct}%`,
                height: "100%",
                background: budgetPct > 90
                  ? "var(--vscode-editorError-foreground, #f44)"
                  : budgetPct > 70
                  ? "var(--vscode-editorWarning-foreground, #fa0)"
                  : "var(--vscode-button-background, #0078d4)",
                borderRadius: 3,
                transition: "width 0.4s ease",
              }}
            />
          </div>
        </div>
      )}

      {/* Model breakdown */}
      {modelBreakdown.length > 0 && (
        <>
          <div style={{ ...sectionHeader, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span>By Model</span>
            <button
              onClick={() => setExpanded((v) => !v)}
              style={{
                background: "transparent",
                border: "none",
                color: "var(--vscode-textLink-foreground, #4af)",
                cursor: "pointer",
                fontSize: 10,
                padding: 0,
              }}
            >
              {expanded ? "Collapse" : "Expand"}
            </button>
          </div>
          {expanded && (
            <div style={{ borderTop: "1px solid var(--vscode-panel-border, #333)", paddingTop: 6 }}>
              {modelBreakdown.map((m, i) => (
                <div key={i} style={{ marginBottom: 8 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                    <span style={{ fontWeight: 600, fontSize: 11 }}>{m.model}</span>
                    <span style={{ ...valueStyle, fontSize: 11 }}>{formatUsd(m.estimated_cost_usd)}</span>
                  </div>
                  <div style={{ display: "flex", gap: 12, fontSize: 10, color: "var(--vscode-descriptionForeground, #888)" }}>
                    <span>{m.provider}</span>
                    <span>{m.call_count} calls</span>
                    <span>{formatTokens(m.input_tokens + m.output_tokens)} tokens</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {/* Empty state */}
      {callCount === 0 && (
        <div style={{ textAlign: "center", color: "var(--vscode-descriptionForeground, #888)", padding: "8px 0", fontSize: 11 }}>
          No LLM calls recorded yet.
        </div>
      )}
    </div>
  );
};

export default CostTracker;
