/**
 * ReCoder — DiagnosticsPanel component
 * Displays First Run diagnostic checklist with activation guides.
 */

import React, { useState } from "react";
import { useVSCodeApi } from "../hooks/useVSCodeApi";

export type ReadyState = string;

export interface DiagnosticsResult {
  core_ready: string;
  ai_ready: string;
  docker_ready: string;
  aws_deploy_ready: string;
  ops_ready: string;
  resolved_model_id?: string;
  resolved_region?: string;
  is_cross_region_profile?: boolean;
  provider_type?: string;
  validation_time?: string;
  details?: Record<string, unknown>;
}

interface DiagnosticsPanelProps {
  diagnostics: DiagnosticsResult | null;
  onRetry?: () => void;
}

interface CheckItem {
  key: keyof DiagnosticsResult;
  label: string;
  description: string;
  activationGuide: string;
  docLink?: string;
  enabledModes: string[];
}

const CHECK_ITEMS: CheckItem[] = [
  {
    key: "core_ready",
    label: "Core Ready",
    description: "Local Python core server is running and reachable.",
    activationGuide: "Run: python -m recoder.core.main in your terminal, or use the ReCoder: Start Core command.",
    docLink: "https://github.com/recoder/docs/core-setup",
    enabledModes: ["Build", "Ship", "Operate"],
  },
  {
    key: "ai_ready",
    label: "AI Ready",
    description: "LLM provider configured (AWS Bedrock or Gemini).",
    activationGuide: "Set AWS credentials for Bedrock, or set GEMINI_API_KEY in your environment. Then reload.",
    docLink: "https://github.com/recoder/docs/ai-setup",
    enabledModes: ["Build", "Ship", "Operate"],
  },
  {
    key: "docker_ready",
    label: "Docker Ready",
    description: "Docker daemon is available and accessible.",
    activationGuide: "Install Docker Desktop or ensure the Docker daemon is running. Verify with: docker info",
    docLink: "https://docs.docker.com/get-docker/",
    enabledModes: ["Ship"],
  },
  {
    key: "aws_deploy_ready",
    label: "AWS Deploy Ready",
    description: "AWS credentials and ECR access are configured.",
    activationGuide: "Configure AWS CLI: aws configure. Ensure IAM role includes ECR and ECS permissions.",
    docLink: "https://github.com/recoder/docs/aws-deploy",
    enabledModes: ["Ship"],
  },
  {
    key: "ops_ready",
    label: "Ops Ready",
    description: "EC2 host and SSH key configured for incident response.",
    activationGuide: "Provide EC2 host address and SSH key path in the Operate tab settings.",
    docLink: "https://github.com/recoder/docs/ops-setup",
    enabledModes: ["Operate"],
  },
];

function getStateIcon(state: ReadyState): string {
  switch (state) {
    case "ready": return "✓";
    case "partial": return "~";
    case "not_ready": return "✗";
    case "error": return "!";
    default: return "?";
  }
}

function getStateColor(state: ReadyState): string {
  switch (state) {
    case "ready": return "var(--vscode-testing-iconPassed, #4caf50)";
    case "partial": return "var(--vscode-editorWarning-foreground, #ff9800)";
    case "not_ready": return "var(--vscode-descriptionForeground, #888)";
    case "error": return "var(--vscode-editorError-foreground, #f44336)";
    default: return "var(--vscode-descriptionForeground, #888)";
  }
}

export const DiagnosticsPanel: React.FC<DiagnosticsPanelProps> = ({
  diagnostics,
  onRetry,
}) => {
  const { postMessage } = useVSCodeApi();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);

  const handleRetry = () => {
    setRetrying(true);
    postMessage("webview.diagnostics.rerun");
    onRetry?.();
    setTimeout(() => setRetrying(false), 3000);
  };

  const containerStyle: React.CSSProperties = {
    background: "var(--vscode-editor-background)",
    border: "1px solid var(--vscode-panel-border, #444)",
    borderRadius: 6,
    padding: "10px 12px",
    fontSize: 12,
    color: "var(--vscode-editor-foreground)",
    fontFamily: "var(--vscode-font-family, sans-serif)",
  };

  if (!diagnostics) {
    return (
      <div style={containerStyle}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div className="spinner" style={{ width: 14, height: 14, border: "2px solid var(--vscode-panel-border, #444)", borderTopColor: "var(--vscode-button-background, #0078d4)", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} />
          <span style={{ color: "var(--vscode-descriptionForeground, #888)" }}>Running diagnostics…</span>
        </div>
        <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
      </div>
    );
  }

  const readyCount = CHECK_ITEMS.filter(
    (item) => diagnostics[item.key] === "ready"
  ).length;

  return (
    <div style={containerStyle}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 13 }}>System Diagnostics</div>
          <div style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #888)", marginTop: 1 }}>
            {readyCount}/{CHECK_ITEMS.length} checks passed
            {diagnostics.validation_time && ` · ${new Date(diagnostics.validation_time).toLocaleTimeString()}`}
          </div>
        </div>
        <button
          onClick={handleRetry}
          disabled={retrying}
          style={{
            background: "var(--vscode-button-secondaryBackground, #3a3a3a)",
            color: "var(--vscode-button-secondaryForeground, #ccc)",
            border: "none",
            borderRadius: 3,
            padding: "4px 10px",
            fontSize: 11,
            cursor: retrying ? "not-allowed" : "pointer",
            opacity: retrying ? 0.6 : 1,
          }}
        >
          {retrying ? "Running…" : "Re-run"}
        </button>
      </div>

      {/* Resolved model info */}
      {diagnostics.resolved_model_id && (
        <div style={{
          background: "var(--vscode-textCodeBlock-background, #1e1e1e)",
          borderRadius: 3,
          padding: "4px 8px",
          fontSize: 11,
          marginBottom: 10,
          fontFamily: "var(--vscode-editor-font-family, monospace)",
          color: "var(--vscode-descriptionForeground, #888)",
        }}>
          Model: <span style={{ color: "var(--vscode-editor-foreground)" }}>{diagnostics.resolved_model_id}</span>
          {diagnostics.resolved_region && (
            <> · Region: <span style={{ color: "var(--vscode-editor-foreground)" }}>{diagnostics.resolved_region}</span></>
          )}
          {diagnostics.is_cross_region_profile && (
            <span style={{ color: "var(--vscode-editorWarning-foreground, #fa0)", marginLeft: 6 }}>[cross-region]</span>
          )}
        </div>
      )}

      {/* Check Items */}
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {CHECK_ITEMS.map((item) => {
          const state: ReadyState = (diagnostics[item.key] as ReadyState) ?? "not_ready";
          const isReady = state === "ready";
          const isOpen = expanded === item.key;
          const color = getStateColor(state);

          return (
            <div
              key={item.key}
              style={{
                border: "1px solid var(--vscode-panel-border, #333)",
                borderRadius: 4,
                overflow: "hidden",
              }}
            >
              {/* Row */}
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "6px 8px",
                  cursor: "pointer",
                  background: isOpen ? "var(--vscode-list-hoverBackground, #2a2a2a)" : "transparent",
                }}
                onClick={() => setExpanded(isOpen ? null : item.key)}
              >
                <span style={{ color, fontWeight: 700, fontSize: 13, width: 14, textAlign: "center", flexShrink: 0 }}>
                  {getStateIcon(state)}
                </span>
                <span style={{ flex: 1, fontWeight: isReady ? 400 : 600, opacity: isReady ? 0.9 : 1 }}>
                  {item.label}
                </span>
                <span style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #888)" }}>
                  {item.enabledModes.join(" / ")}
                </span>
                <span style={{ fontSize: 10, color: "var(--vscode-descriptionForeground, #888)" }}>
                  {isOpen ? "▲" : "▼"}
                </span>
              </div>

              {/* Expanded details */}
              {isOpen && (
                <div style={{
                  padding: "8px 10px 10px",
                  borderTop: "1px solid var(--vscode-panel-border, #333)",
                  background: "var(--vscode-sideBar-background, #252526)",
                }}>
                  <div style={{ color: "var(--vscode-descriptionForeground, #888)", marginBottom: 6, lineHeight: 1.5 }}>
                    {item.description}
                  </div>

                  {!isReady && (
                    <div style={{
                      background: "var(--vscode-textCodeBlock-background, #1e1e1e)",
                      borderRadius: 3,
                      padding: "6px 8px",
                      fontSize: 11,
                      lineHeight: 1.6,
                      color: "var(--vscode-editor-foreground)",
                      marginBottom: 6,
                    }}>
                      <strong>How to activate:</strong> {item.activationGuide}
                    </div>
                  )}

                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    {item.docLink && (
                      <a
                        href={item.docLink}
                        target="_blank"
                        rel="noreferrer"
                        style={{ color: "var(--vscode-textLink-foreground, #4af)", fontSize: 11 }}
                        onClick={(e) => {
                          e.preventDefault();
                          postMessage("webview.open.external", { url: item.docLink });
                        }}
                      >
                        View docs →
                      </a>
                    )}
                    {!isReady && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          postMessage("webview.diagnostics.fix", { key: item.key });
                        }}
                        style={{
                          background: "transparent",
                          border: "1px solid var(--vscode-panel-border, #444)",
                          color: "var(--vscode-textLink-foreground, #4af)",
                          borderRadius: 3,
                          padding: "2px 8px",
                          fontSize: 10,
                          cursor: "pointer",
                        }}
                      >
                        Retry check
                      </button>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default DiagnosticsPanel;
