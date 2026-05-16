/**
 * ReCoder Webview — Root App Component
 *
 * 3-Mode tab layout:
 *   Build (에러 분석 · 코드 패치)
 *   Ship  (Dockerfile 생성 · docker build/run · Health Check)
 *   Operate (EC2 인시던트 조회 · 운영 대응)
 *
 * Also renders:
 *   DiagnosticsPanel (First Run 진단 상태)
 *   CostTracker      (우하단 비용 표시)
 */

import React, { useState, useCallback, useEffect } from "react";
import { useVSCodeApi } from "./hooks/useVSCodeApi";
import { usePolling } from "./hooks/usePolling";
import { BuildMode } from "./components/BuildMode";
import { ShipMode } from "./components/ShipMode";
import { OperateMode } from "./components/OperateMode";
import { DiagnosticsPanel } from "./components/DiagnosticsPanel";
import { CostTracker } from "./components/CostTracker";

// ---------------------------------------------------------------------------
// Types (mirrored from extension/src/types.ts)
// ---------------------------------------------------------------------------

type TabMode = "build" | "ship" | "operate";

interface DiagnosticsResult {
  core_ready: string;
  ai_ready: string;
  docker_ready: string;
  aws_deploy_ready: string;
  ops_ready: string;
}

// ---------------------------------------------------------------------------
// Tab bar
// ---------------------------------------------------------------------------

interface TabBarProps {
  current: TabMode;
  diagnostics: DiagnosticsResult | null;
  onChange: (mode: TabMode) => void;
}

const TabBar: React.FC<TabBarProps> = ({ current, diagnostics, onChange }) => {
  const tabs: { id: TabMode; label: string; enabled: boolean }[] = [
    {
      id: "build",
      label: "Build",
      enabled: diagnostics ? diagnostics.ai_ready === "ready" : true,
    },
    {
      id: "ship",
      label: "Ship",
      enabled: diagnostics
        ? diagnostics.ai_ready === "ready"
        : true,
    },
    {
      id: "operate",
      label: "Operate",
      enabled: diagnostics
        ? diagnostics.ai_ready === "ready" &&
          diagnostics.aws_deploy_ready === "ready" &&
          diagnostics.ops_ready === "ready"
        : false,
    },
  ];

  return (
    <div
      style={{
        display: "flex",
        borderBottom: "1px solid var(--vscode-panel-border, #333)",
        background: "var(--vscode-editorGroupHeader-tabsBackground, #2d2d2d)",
      }}
    >
      {tabs.map((tab) => (
        <button
          key={tab.id}
          onClick={() => tab.enabled && onChange(tab.id)}
          disabled={!tab.enabled}
          style={{
            flex: 1,
            padding: "8px 4px",
            border: "none",
            borderBottom:
              current === tab.id
                ? "2px solid var(--vscode-focusBorder, #0078d4)"
                : "2px solid transparent",
            background: "transparent",
            color:
              current === tab.id
                ? "var(--vscode-tab-activeForeground, #fff)"
                : tab.enabled
                ? "var(--vscode-tab-inactiveForeground, #999)"
                : "var(--vscode-disabledForeground, #555)",
            cursor: tab.enabled ? "pointer" : "not-allowed",
            fontSize: 12,
            fontWeight: current === tab.id ? 700 : 400,
            transition: "color 0.15s",
          }}
          title={!tab.enabled ? "추가 설정 필요 — 진단 탭에서 확인" : undefined}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
};

// ---------------------------------------------------------------------------
// CoreStatus indicator
// ---------------------------------------------------------------------------

interface CoreStatusProps {
  status: "ok" | "degraded" | "down" | null;
  uptime: number;
}

const CoreStatus: React.FC<CoreStatusProps> = ({ status, uptime }) => {
  const color =
    status === "ok"
      ? "var(--vscode-testing-iconPassed, #4caf50)"
      : status === "degraded"
      ? "var(--vscode-editorWarning-foreground, #ff9800)"
      : "var(--vscode-editorError-foreground, #f44336)";

  const label =
    status === "ok"
      ? "Core 정상"
      : status === "degraded"
      ? "Core 저하"
      : status === null
      ? "연결 중…"
      : "Core 오프라인";

  const formatUptime = (seconds: number) => {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    return `${Math.round(seconds / 3600)}h`;
  };

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 5,
        padding: "4px 8px",
        borderBottom: "1px solid var(--vscode-panel-border, #333)",
        fontSize: 10,
        color: "var(--vscode-descriptionForeground, #888)",
      }}
    >
      <span
        style={{
          width: 7,
          height: 7,
          borderRadius: "50%",
          background: color,
          display: "inline-block",
          flexShrink: 0,
        }}
      />
      <span style={{ color }}>{label}</span>
      {status === "ok" && uptime > 0 && (
        <span style={{ marginLeft: "auto" }}>↑ {formatUptime(uptime)}</span>
      )}
    </div>
  );
};

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

const App: React.FC = () => {
  const { postMessage, useMessage } = useVSCodeApi();
  const { coreHealth, costSummary } = usePolling(4000);

  const [currentMode, setCurrentMode] = useState<TabMode>("build");
  const [diagnostics, setDiagnostics] = useState<DiagnosticsResult | null>(null);
  const [showDiagnostics, setShowDiagnostics] = useState(false);

  // Listen for messages from extension host
  useMessage(
    useCallback((msg) => {
      const { type, payload } = msg;

      if (type === "stateUpdate") {
        const state = payload as { currentMode?: string; diagnostics?: DiagnosticsResult };
        if (state.currentMode) {
          setCurrentMode(state.currentMode as TabMode);
        }
        if (state.diagnostics) {
          setDiagnostics(state.diagnostics);
        }
      }

      if (type === "diagnosticsUpdate") {
        setDiagnostics(payload as DiagnosticsResult);
      }
    }, [])
  );

  // On mount, request diagnostics
  useEffect(() => {
    postMessage("runDiagnostics", {});
  }, [postMessage]);

  const isAiReady = diagnostics ? diagnostics.ai_ready === "ready" : false;
  const isDockerReady = diagnostics ? diagnostics.docker_ready === "ready" : false;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100vh",
        overflow: "hidden",
      }}
    >
      {/* Core status bar */}
      <CoreStatus
        status={coreHealth?.status ?? null}
        uptime={coreHealth?.uptime ?? 0}
      />

      {/* Mode tabs */}
      <TabBar
        current={currentMode}
        diagnostics={diagnostics}
        onChange={setCurrentMode}
      />

      {/* Diagnostics toggle */}
      <div
        style={{
          padding: "2px 8px",
          borderBottom: "1px solid var(--vscode-panel-border, #222)",
          display: "flex",
          justifyContent: "flex-end",
        }}
      >
        <button
          onClick={() => {
            setShowDiagnostics((v) => !v);
            if (!showDiagnostics) {
              postMessage("runDiagnostics", {});
            }
          }}
          style={{
            background: "none",
            border: "none",
            color: "var(--vscode-textLink-foreground, #4af)",
            fontSize: 10,
            cursor: "pointer",
            padding: "2px 0",
          }}
        >
          {showDiagnostics ? "▲ 진단 숨기기" : "▼ 시스템 진단"}
        </button>
      </div>

      {/* Diagnostics panel (collapsible) */}
      {showDiagnostics && (
        <div
          style={{
            borderBottom: "1px solid var(--vscode-panel-border, #333)",
            maxHeight: 220,
            overflowY: "auto",
          }}
        >
          <DiagnosticsPanel diagnostics={diagnostics} />
        </div>
      )}

      {/* Mode content area */}
      <div style={{ flex: 1, overflowY: "auto", padding: "8px" }}>
        {currentMode === "build" && (
          <BuildMode isActive={isAiReady} />
        )}
        {currentMode === "ship" && (
          <ShipMode isAiReady={isAiReady} isDockerReady={isDockerReady} />
        )}
        {currentMode === "operate" && (
          <OperateMode
            isActive={
              isAiReady &&
              diagnostics?.aws_deploy_ready === "ready" &&
              diagnostics?.ops_ready === "ready"
            }
          />
        )}
      </div>

      {/* Cost tracker — fixed bottom */}
      <div
        style={{
          borderTop: "1px solid var(--vscode-panel-border, #333)",
          padding: "3px 8px",
          background: "var(--vscode-editorGroupHeader-tabsBackground, #2d2d2d)",
        }}
      >
        <CostTracker costSummary={costSummary} />
      </div>
    </div>
  );
};

export default App;
