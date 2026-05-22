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
// Status Pills (Core / AI / Docker)
// ---------------------------------------------------------------------------

interface StatusPillsProps {
  diagnostics: DiagnosticsResult | null;
  coreStatus: "ok" | "degraded" | "down" | null;
}

const StatusPills: React.FC<StatusPillsProps> = ({ diagnostics, coreStatus }) => {
  const green = "#22c55e";
  const red = "#ef4444";
  const gray = "#6b7280";

  const coreOk = coreStatus === "ok";
  const aiOk = diagnostics?.ai_ready === "ready";
  const dockerOk = diagnostics?.docker_ready === "ready";

  const pill = (label: string, ok: boolean | null): React.CSSProperties => ({
    display: "inline-flex",
    alignItems: "center",
    gap: 5,
    padding: "3px 10px",
    borderRadius: 20,
    border: `1.5px solid ${ok === null ? gray : ok ? green : red}`,
    background: ok === null ? "rgba(107,114,128,0.15)" : ok ? "rgba(34,197,94,0.15)" : "rgba(239,68,68,0.15)",
    color: ok === null ? gray : ok ? green : red,
    fontSize: 11,
    fontWeight: 600,
    cursor: "default",
    userSelect: "none",
  });

  const dot = (ok: boolean | null): React.CSSProperties => ({
    width: 6,
    height: 6,
    borderRadius: "50%",
    background: ok === null ? gray : ok ? green : red,
    flexShrink: 0,
  });

  return (
    <div style={{
      display: "flex",
      gap: 6,
      padding: "8px 10px 6px",
      background: "var(--vscode-sideBar-background, #1e1e1e)",
      flexWrap: "wrap",
    }}>
      <span style={pill("Core", coreOk)}>
        <span style={dot(coreOk)} />
        Core {coreOk ? "✓" : coreStatus === null ? "…" : "✗"}
      </span>
      <span style={pill("AI", aiOk ?? null)}>
        <span style={dot(aiOk ?? null)} />
        AI {aiOk ? "✓" : diagnostics === null ? "…" : "✗"}
      </span>
      <span style={pill("Docker", dockerOk ?? null)}>
        <span style={dot(dockerOk ?? null)} />
        Docker {dockerOk ? "✓" : diagnostics === null ? "…" : "✗"}
      </span>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Tab bar
// ---------------------------------------------------------------------------

interface TabBarProps {
  current: TabMode;
  diagnostics: DiagnosticsResult | null;
  onChange: (mode: TabMode) => void;
}

const TAB_ICONS: Record<TabMode, string> = {
  build: "⚙",
  ship: "🚢",
  operate: "⊙",
};

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
      enabled: diagnostics ? diagnostics.ai_ready === "ready" : true,
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
        borderBottom: "2px solid var(--vscode-panel-border, #2a2a2a)",
        background: "var(--vscode-sideBar-background, #1e1e1e)",
      }}
    >
      {tabs.map((tab) => {
        const isActive = current === tab.id;
        return (
          <button
            key={tab.id}
            onClick={() => tab.enabled && onChange(tab.id)}
            disabled={!tab.enabled}
            style={{
              flex: 1,
              padding: "9px 4px 8px",
              border: "none",
              borderBottom: isActive
                ? "2px solid #3b82f6"
                : "2px solid transparent",
              marginBottom: -2,
              background: isActive ? "rgba(59,130,246,0.08)" : "transparent",
              color: isActive
                ? "#ffffff"
                : tab.enabled
                ? "var(--vscode-tab-inactiveForeground, #999)"
                : "var(--vscode-disabledForeground, #444)",
              cursor: tab.enabled ? "pointer" : "not-allowed",
              fontSize: 12,
              fontWeight: isActive ? 700 : 400,
              transition: "all 0.15s",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 5,
            }}
            title={!tab.enabled ? "추가 설정 필요 — 진단 탭에서 확인" : undefined}
          >
            <span style={{ fontSize: 13 }}>{TAB_ICONS[tab.id]}</span>
            {tab.label}
          </button>
        );
      })}
    </div>
  );
};

// (CoreStatus replaced by StatusPills above)

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
        background: "var(--vscode-sideBar-background, #1e1e1e)",
      }}
    >
      {/* RECODER header */}
      <div style={{
        padding: "10px 12px 4px",
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: "0.12em",
        color: "var(--vscode-descriptionForeground, #888)",
        textTransform: "uppercase",
        userSelect: "none",
      }}>
        RECODER
      </div>

      {/* Status Pills */}
      <StatusPills diagnostics={diagnostics} coreStatus={coreHealth?.status ?? null} />

      {/* Mode tabs */}
      <TabBar
        current={currentMode}
        diagnostics={diagnostics}
        onChange={setCurrentMode}
      />

      {/* Diagnostics panel (collapsible via small link) */}
      <div style={{ display: "flex", justifyContent: "flex-end", padding: "2px 10px 0" }}>
        <button
          onClick={() => {
            setShowDiagnostics((v) => !v);
            if (!showDiagnostics) postMessage("runDiagnostics", {});
          }}
          style={{
            background: "none",
            border: "none",
            color: "var(--vscode-textLink-foreground, #4a9eff)",
            fontSize: 10,
            cursor: "pointer",
            padding: "2px 0",
            opacity: 0.6,
          }}
        >
          {showDiagnostics ? "▲ 진단 숨기기" : "▼ 시스템 진단"}
        </button>
      </div>

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
      <div style={{ flex: 1, overflowY: "auto", padding: "10px 10px 8px" }}>
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
          borderTop: "1px solid var(--vscode-panel-border, #2a2a2a)",
          padding: "3px 10px",
          background: "var(--vscode-sideBar-background, #1e1e1e)",
        }}
      >
        <CostTracker costSummary={costSummary} />
      </div>
    </div>
  );
};

export default App;
